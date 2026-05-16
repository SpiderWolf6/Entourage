"""FinalReviewer — runs after all sprints to fix fatal errors and produce a build verdict.

Two-phase operation:
1. FIX phase: Claude reads every file in the workspace and fixes crashes / broken imports /
   logic errors that would prevent the app from running. It does NOT refactor or add features.
2. VERDICT phase: Claude reads the original user story + all planning artifacts + project state
   and produces a structured scorecard judging how well the pipeline delivered on the brief.

The review output is streamed back via the same EventBus as all other execution events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)

REVIEWER_TIMEOUT = 1800  # 30 minutes — reviewer reads the whole codebase

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ReviewResult:
    """Output of the final review pass."""
    status: str                  # "done" | "error" | "timeout"
    fixes_applied: list[str]     # List of files that were modified
    verdict: "BuildVerdict | None" = None
    raw_output: str = ""
    error: str = ""


@dataclass
class BuildVerdict:
    """Structured scorecard produced by the reviewer."""
    overall_score: int           # 1-10
    delivered: list[str]         # Features/requirements successfully built
    missing: list[str]           # Features/requirements not delivered
    fatal_fixes: list[str]       # Fatal errors that were auto-fixed
    bugs_remaining: list[str]    # Known issues that couldn't be fixed
    fidelity_summary: str        # 2-3 sentence overall assessment
    recommendation: str          # "ship" | "iterate" | "rebuild"


# ─── Main reviewer class ──────────────────────────────────────────────────────

class FinalReviewer:
    """Runs the final code review and verdict pass on a completed workspace."""

    def __init__(self, workspace_dir: Path, project_id: str):
        self.workspace_dir = workspace_dir
        self.project_id = project_id
        self._claude_bin = _find_claude_binary()

    async def run(
        self,
        user_story: str,
        planning_artifacts: dict[str, str],   # agent_name -> artifact text
        on_event: EventCallback | None = None,
    ) -> ReviewResult:
        """Run the full review. Emits reviewer_stream events during progress."""

        if not self._claude_bin:
            return ReviewResult(
                status="error",
                fixes_applied=[],
                error="claude CLI not found",
            )

        await _emit(on_event, "reviewer_start", {"message": "Reviewer starting — scanning codebase for fatal errors..."})

        # Snapshot before
        pre_files = set(_snapshot_workspace(self.workspace_dir).keys())

        # Build the review prompt
        prompt = _build_reviewer_prompt(
            user_story=user_story,
            planning_artifacts=planning_artifacts,
            workspace_dir=self.workspace_dir,
        )

        # Run claude in the workspace
        loop = asyncio.get_event_loop()
        prompt_bytes = prompt.encode("utf-8")

        cmd = _build_claude_command(self._claude_bin, self.workspace_dir)

        def _run_blocking() -> dict:
            result = subprocess.run(
                cmd,
                input=prompt_bytes,
                capture_output=True,
                cwd=str(self.workspace_dir),
                env=_build_env(self.workspace_dir),
                timeout=REVIEWER_TIMEOUT,
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout.decode("utf-8", errors="replace"),
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }

        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, _run_blocking),
                timeout=REVIEWER_TIMEOUT + 30,
            )
        except asyncio.TimeoutError:
            await _emit(on_event, "reviewer_done", {"status": "timeout"})
            return ReviewResult(status="timeout", fixes_applied=[], error="Reviewer timed out")
        except Exception as e:
            await _emit(on_event, "reviewer_done", {"status": "error", "error": str(e)})
            return ReviewResult(status="error", fixes_applied=[], error=str(e))

        # Parse stream-json output
        full_text_parts: list[str] = []
        reviewer_cost = 0.0
        for line in raw["stdout"].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get("type") == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "").strip()
                            if t:
                                full_text_parts.append(t)
                                # Stream chunks to frontend
                                await _emit(on_event, "reviewer_stream", {"text": t[:400]})
                elif ev.get("type") == "result":
                    reviewer_cost = float(ev.get("total_cost_usd", ev.get("cost_usd", 0)) or 0)
            except Exception:
                pass

        full_text = "\n\n".join(full_text_parts)

        # Diff workspace to find what changed
        post_files = set(_snapshot_workspace(self.workspace_dir).keys())
        changed = [f for f in post_files if f in pre_files] + list(post_files - pre_files)
        # Narrow to files actually modified (mtime changed)
        pre_snap = _snapshot_workspace(self.workspace_dir)  # post-run snap
        fixes_applied = _find_modified_files(self.workspace_dir, pre_files, pre_snap)

        # Parse the structured verdict out of full_text
        verdict = _parse_verdict(full_text)

        await _emit(on_event, "reviewer_done", {
            "status": "done",
            "fixes_count": len(fixes_applied),
            "fixes": fixes_applied[:10],
            "score": verdict.overall_score if verdict else None,
            "verdict": _verdict_to_dict(verdict) if verdict else None,
            "cost_usd": reviewer_cost,
        })

        return ReviewResult(
            status="done",
            fixes_applied=fixes_applied,
            verdict=verdict,
            raw_output=full_text,
        )


# ─── Prompt builder ───────────────────────────────────────────────────────────

def _build_reviewer_prompt(
    user_story: str,
    planning_artifacts: dict[str, str],
    workspace_dir: Path,
) -> str:
    lines: list[str] = []

    lines.append("# Final Code Review — Two Phases")
    lines.append("")
    lines.append("You are the Final Reviewer for a fully AI-generated codebase.")
    lines.append("You have two jobs. Do them in order.")
    lines.append("")

    # ── Phase 1: Fix fatal errors ──
    lines.append("## PHASE 1: FIX FATAL ERRORS")
    lines.append("")
    lines.append("Read every source file in this workspace. Fix ONLY things that would cause:")
    lines.append("- ImportError / ModuleNotFoundError (wrong package name, missing import)")
    lines.append("- SyntaxError (broken Python or JavaScript syntax)")
    lines.append("- NameError / AttributeError on startup (undefined variable used immediately)")
    lines.append("- Flask route returning nothing / missing return statement")
    lines.append("- React component with JSX syntax error or missing closing tag")
    lines.append("- API endpoint URL mismatch between frontend fetch() call and Flask route")
    lines.append("- Missing required file that is imported by another file")
    lines.append("- requirements.txt package name wrong (e.g. 'sqlalchemy' instead of 'flask-sqlalchemy')")
    lines.append("")
    lines.append("Do NOT: refactor, rename, add features, improve styling, or change anything that works.")
    lines.append("Do NOT: add comments explaining what you changed.")
    lines.append("ONLY fix crashes. A working ugly app beats a broken beautiful one.")
    lines.append("")
    lines.append("For each file you fix, use the Edit tool. For missing files, use Write.")
    lines.append("After fixing, list what you changed in this format:")
    lines.append("")
    lines.append("FIXES_APPLIED:")
    lines.append("- <filename>: <one sentence description of the bug fixed>")
    lines.append("(Write 'FIXES_APPLIED: none' if the code is already clean.)")
    lines.append("")

    # ── Phase 2: Verdict ──
    lines.append("## PHASE 2: BUILD VERDICT")
    lines.append("")
    lines.append("Now act as a senior engineering reviewer writing an honest, objective post-mortem.")
    lines.append("Judge how well the AI pipeline delivered on what it AGREED to build.")
    lines.append("")
    lines.append("IMPORTANT: Do NOT grade against the raw user request below. Grade against the")
    lines.append("Engineering Manager brief and Product Requirements — those define what was actually")
    lines.append("scoped and committed to. The user story is background context only.")
    lines.append("")
    lines.append("### Original User Request (background context only — do NOT grade against this)")
    lines.append(user_story.strip())
    lines.append("")

    # Include key planning artifacts (truncated to keep within token budget)
    em_brief = planning_artifacts.get("engineering_manager", "")
    po_reqs = planning_artifacts.get("product_owner", "")
    arch = planning_artifacts.get("architect", "")

    if em_brief:
        lines.append("### Engineering Manager Brief (this is the agreed scope — grade against this)")
        lines.append(em_brief[:1200].strip())
        lines.append("")
    if po_reqs:
        lines.append("### Product Requirements (PO) (the committed feature list — grade against this)")
        lines.append(po_reqs[:1500].strip())
        lines.append("")
    if arch:
        lines.append("### Architecture Decisions")
        lines.append(arch[:1000].strip())
        lines.append("")

    lines.append("### Codebase You Built")
    lines.append("(You just read all the files above — use that knowledge here.)")
    lines.append("")
    lines.append("Write your verdict in EXACTLY this format:")
    lines.append("")
    lines.append("VERDICT:")
    lines.append("SCORE: <integer 1-10>")
    lines.append("RECOMMENDATION: <one of: ship | iterate | rebuild>")
    lines.append("")
    lines.append("DELIVERED:")
    lines.append("- <feature or requirement that was successfully built>")
    lines.append("- ...")
    lines.append("")
    lines.append("MISSING:")
    lines.append("- <feature or requirement from the brief that was NOT built, or was built incorrectly>")
    lines.append("- ...")
    lines.append("(Write 'none' if everything was delivered)")
    lines.append("")
    lines.append("BUGS_REMAINING:")
    lines.append("- <non-fatal bug or UX issue you noticed but did not fix>")
    lines.append("- ...")
    lines.append("(Write 'none' if no remaining issues)")
    lines.append("")
    lines.append("FIDELITY_SUMMARY:")
    lines.append("<2-4 sentences. Be direct and specific. Call out exactly what worked, what didn't,")
    lines.append("and why. Do not use filler phrases like 'overall' or 'in conclusion'.")
    lines.append("Write as if you are briefing the founder who commissioned this build.>")
    lines.append("")
    lines.append("SCORING RUBRIC:")
    lines.append("10 = Ships exactly as requested, all features work, no bugs")
    lines.append("8-9 = Core product works end-to-end, minor issues only")
    lines.append("6-7 = Main features present but some broken or missing")
    lines.append("4-5 = Partial implementation, significant gaps")
    lines.append("1-3 = Fundamentally broken or barely resembles the request")

    return "\n".join(lines)


# ─── Verdict parser ───────────────────────────────────────────────────────────

def _parse_verdict(text: str) -> BuildVerdict | None:
    """Parse the structured verdict block from claude's output."""
    if "VERDICT:" not in text:
        return None

    try:
        # Extract score
        import re
        score_m = re.search(r"SCORE:\s*(\d+)", text)
        score = int(score_m.group(1)) if score_m else 5
        score = max(1, min(10, score))

        # Extract recommendation
        rec_m = re.search(r"RECOMMENDATION:\s*(ship|iterate|rebuild)", text, re.IGNORECASE)
        recommendation = rec_m.group(1).lower() if rec_m else "iterate"

        # Extract list sections
        def _extract_list(section: str) -> list[str]:
            m = re.search(rf"{section}:\n(.*?)(?=\n[A-Z_]+:|\Z)", text, re.DOTALL)
            if not m:
                return []
            raw = m.group(1).strip()
            if raw.lower() == "none":
                return []
            items = []
            for line in raw.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line and len(line) > 3:
                    items.append(line)
            return items

        delivered = _extract_list("DELIVERED")
        missing = _extract_list("MISSING")
        bugs = _extract_list("BUGS_REMAINING")

        # Extract fixes from FIXES_APPLIED section
        fixes_m = re.search(r"FIXES_APPLIED:\n(.*?)(?=\n##|\n[A-Z_]+:|\Z)", text, re.DOTALL)
        fixes: list[str] = []
        if fixes_m:
            raw = fixes_m.group(1).strip()
            if raw.lower() != "none":
                for line in raw.split("\n"):
                    line = line.strip().lstrip("- ").strip()
                    if line and len(line) > 3:
                        fixes.append(line)

        # Extract fidelity summary
        fid_m = re.search(r"FIDELITY_SUMMARY:\n(.*?)(?=\n[A-Z_]+:|\Z)", text, re.DOTALL)
        fidelity = fid_m.group(1).strip() if fid_m else "No summary produced."

        return BuildVerdict(
            overall_score=score,
            delivered=delivered,
            missing=missing,
            fatal_fixes=fixes,
            bugs_remaining=bugs,
            fidelity_summary=fidelity,
            recommendation=recommendation,
        )

    except Exception as e:
        log.warning("Could not parse reviewer verdict: %s", e)
        return None


def _verdict_to_dict(v: BuildVerdict) -> dict:
    return {
        "score": v.overall_score,
        "recommendation": v.recommendation,
        "delivered": v.delivered,
        "missing": v.missing,
        "fatal_fixes": v.fatal_fixes,
        "bugs_remaining": v.bugs_remaining,
        "fidelity_summary": v.fidelity_summary,
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_modified_files(workspace_dir: Path, pre_file_set: set[str], post_snap: dict[str, str]) -> list[str]:
    """Return list of files that changed or were created since pre_file_set was taken."""
    created = [f for f in post_snap if f not in pre_file_set]
    return created


def _snapshot_workspace(workspace_dir: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not workspace_dir.exists():
        return snap
    SKIP = {".venv", "node_modules", "__pycache__", ".git", ".claude"}
    SKIP_EXT = {".pyc", ".pyo", ".db", ".sqlite", ".lock", ".map"}
    for path in workspace_dir.rglob("*"):
        if path.is_file():
            parts = path.relative_to(workspace_dir).parts
            if any(p in SKIP for p in parts):
                continue
            if path.suffix.lower() in SKIP_EXT:
                continue
            try:
                st = path.stat()
                snap[str(path.relative_to(workspace_dir))] = f"{st.st_mtime}:{st.st_size}"
            except OSError:
                pass
    return snap


def _build_claude_command(claude_bin: str, workspace_dir: Path) -> list[str]:
    return [
        claude_bin,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--add-dir", str(workspace_dir),
        "--no-session-persistence",
    ]


def _build_env(workspace_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_WORKSPACE"] = str(workspace_dir)
    env["CLAUDE_DISABLE_AUTOUPDATE"] = "1"
    return env


def _find_claude_binary() -> str | None:
    candidates = ["claude.cmd", "claude.exe", "claude"] if platform.system() == "Windows" else ["claude"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    try:
        result = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            prefix = result.stdout.strip()
            candidate = Path(prefix) / ("claude.cmd" if platform.system() == "Windows" else "bin/claude")
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    return None


async def _emit(cb: EventCallback | None, event_type: str, data: dict[str, Any]) -> None:
    if cb:
        try:
            await cb(event_type, data)
        except Exception as e:
            log.warning("Reviewer emit error %s: %s", event_type, e)
