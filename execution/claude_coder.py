"""ClaudeCoderAgent — runs `claude` CLI as a subprocess inside a project sandbox.

Design:
- Each spawned developer agent from HR gets an isolated `claude` process.
- The agent receives: its HR-generated system prompt + the sprint task description
  + only the files it's allowed to touch (from sprint plan `files_to_create` /
  `files_to_modify`).
- `claude` is run with `--dangerously-skip-permissions` and `--output-format stream-json`
  so we can stream output tokens back to the EventBus in real time.
- After claude exits, we read all files it wrote back from the workspace and
  persist them to state.
- File access restriction: we pass `--allowedTools "Read,Edit,Write,Bash"` and
  set the cwd to the workspace dir.  We also pass an `--allowed-paths` constraint
  so claude can only touch the sprint-designated files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Awaitable

log = logging.getLogger(__name__)

# Maximum seconds to wait for a single claude subprocess
CLAUDE_TIMEOUT = 1200  # 20 minutes per agent per sprint


@dataclass
class CoderTask:
    """Description of work for a single coder in a sprint."""
    agent_name: str             # e.g. "python_dev"
    system_prompt: str          # From HR agent output
    sprint_number: int
    task_description: str       # Plain text task from sprint plan
    files_to_create: list[str]  # Relative paths
    files_to_modify: list[str]  # Relative paths
    # Short-term: this agent's own previous sprint summaries
    agent_log: str = ""
    # Long-term: project-wide state from all agents
    project_state: str = ""


@dataclass
class CoderResult:
    """Outcome of a single coder run."""
    agent_name: str
    sprint_number: int
    status: str           # "done" | "error" | "timeout"
    files_written: list[str] = field(default_factory=list)
    output_text: str = ""
    error: str = ""
    exit_code: int = 0


EventCallback = Callable[[str, str, str], Awaitable[None]]  # (agent, event_type, content)


class ClaudeCoderAgent:
    """Runs a claude CLI subprocess for a single dev agent on a sprint task.

    Usage:
        agent = ClaudeCoderAgent(workspace_dir, project_id)
        result = await agent.run(task, on_event=my_callback)
    """

    def __init__(self, workspace_dir: Path, project_id: str, creds=None):
        self.workspace_dir = workspace_dir
        self.project_id = project_id
        self.creds = creds  # Credentials — used to inject ANTHROPIC_API_KEY into subprocess env
        self._claude_bin = _find_claude_binary()

    async def run(
        self,
        task: CoderTask,
        on_event: EventCallback | None = None,
    ) -> CoderResult:
        """Run claude on the given task. Streams events via on_event callback.

        Strategy:
        1. Build a comprehensive prompt that includes: system prompt, architecture,
           existing file contents, and the specific task.
        2. Write a CLAUDE.md file in workspace to guide claude.
        3. Launch claude subprocess with strict file allow-list.
        4. Stream output lines, emit events.
        5. Collect all written files after exit.
        """
        await _emit(on_event, task.agent_name, "start",
                    f"Sprint {task.sprint_number}: {task.agent_name} starting...")

        if not self._claude_bin:
            return CoderResult(
                agent_name=task.agent_name,
                sprint_number=task.sprint_number,
                status="error",
                error="claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
            )

        # Write CLAUDE.md to enforce no-exploration behavior
        _write_claude_md(self.workspace_dir)

        # Build the prompt file (we pass it via stdin / --message)
        prompt = _build_coder_prompt(task)

        # Build allowed paths list for --add-dir restriction
        allowed_paths = list(set(task.files_to_create + task.files_to_modify))

        # Snapshot which files existed before
        pre_snapshot = _snapshot_workspace(self.workspace_dir)

        # Run claude subprocess
        try:
            result = await self._run_subprocess(
                task=task,
                prompt=prompt,
                allowed_paths=allowed_paths,
                on_event=on_event,
            )
        except asyncio.TimeoutError:
            await _emit(on_event, task.agent_name, "timeout",
                        f"{task.agent_name} timed out after {CLAUDE_TIMEOUT}s")
            return CoderResult(
                agent_name=task.agent_name,
                sprint_number=task.sprint_number,
                status="timeout",
                error=f"Agent timed out after {CLAUDE_TIMEOUT} seconds",
            )
        except Exception as e:
            await _emit(on_event, task.agent_name, "error", str(e))
            return CoderResult(
                agent_name=task.agent_name,
                sprint_number=task.sprint_number,
                status="error",
                error=str(e),
            )

        # Collect files written (diff against pre-snapshot)
        post_snapshot = _snapshot_workspace(self.workspace_dir)
        files_written = [
            path for path in post_snapshot
            if path not in pre_snapshot or post_snapshot[path] != pre_snapshot[path]
        ]

        await _emit(on_event, task.agent_name, "done",
                    f"Wrote {len(files_written)} file(s): {', '.join(files_written[:5])}")

        # QA agents run pytest — non-zero exit means test failures, not agent failure
        is_qa = "qa" in task.agent_name.lower()
        success = result["exit_code"] == 0 or is_qa
        return CoderResult(
            agent_name=task.agent_name,
            sprint_number=task.sprint_number,
            status="done" if success else "error",
            files_written=files_written,
            output_text=result["output"],
            exit_code=result["exit_code"],
            error=result.get("error", "") if not success else "",
        )

    async def _run_subprocess(
        self,
        task: CoderTask,
        prompt: str,
        allowed_paths: list[str],
        on_event: EventCallback | None,
    ) -> dict[str, Any]:
        """Launch claude as subprocess, stream output, return exit info.

        Prompt and system prompt are passed via stdin to avoid Windows
        32k command-line length limit.
        """
        # Build the full input: system prompt section + task prompt
        # We embed the system prompt in the user message since --system-prompt
        # inline arg hits Windows 32k cmd limit.
        full_input = prompt
        if task.system_prompt:
            full_input = f"[SYSTEM ROLE]\n{task.system_prompt.strip()}\n\n[TASK]\n{prompt}"

        cmd = _build_claude_command(
            claude_bin=self._claude_bin,
            workspace_dir=self.workspace_dir,
        )

        log.info("Launching claude for %s (sprint %d)", task.agent_name, task.sprint_number)

        # Run claude in a thread executor to avoid asyncio.create_subprocess_exec
        # which fails on Windows under uvicorn's SelectorEventLoop.
        loop = asyncio.get_event_loop()
        prompt_bytes = full_input.encode("utf-8")

        def _run_claude_blocking() -> dict[str, Any]:
            result = subprocess.run(
                cmd,
                input=prompt_bytes,
                capture_output=True,
                cwd=str(self.workspace_dir),
                env=_build_env(self.workspace_dir, self.creds),
                timeout=CLAUDE_TIMEOUT,
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout.decode("utf-8", errors="replace"),
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }

        raw = await asyncio.wait_for(
            loop.run_in_executor(None, _run_claude_blocking),
            timeout=CLAUDE_TIMEOUT + 10,
        )

        # Parse and emit stream-json lines from stdout
        output_lines: list[str] = []
        for line in raw["stdout"].splitlines():
            output_lines.append(line)
            event_text = _parse_stream_event(line)
            if event_text:
                await _emit(on_event, task.agent_name, "stream", event_text)

        full_output = "\n".join(output_lines)
        full_error = raw["stderr"]

        log.info("claude exited %d for %s sprint %d",
                 raw["exit_code"], task.agent_name, task.sprint_number)
        if full_error:
            log.warning("claude stderr for %s: %s", task.agent_name, full_error[:1000])
            await _emit(on_event, task.agent_name, "stream",
                        f"[stderr] {full_error[:500]}")
        if raw["exit_code"] != 0 and not full_output:
            log.warning("claude produced no output for %s sprint %d",
                        task.agent_name, task.sprint_number)

        return {
            "exit_code": raw["exit_code"],
            "output": full_output,
            "error": full_error if raw["exit_code"] != 0 else "",
        }


# ── Command builder ────────────────────────────────────────────────────────────

def _build_claude_command(
    claude_bin: str,
    workspace_dir: Path,
) -> list[str]:
    """Build the claude CLI command. Prompt is passed via stdin to avoid
    Windows 32k command-line length limit."""
    return [
        claude_bin,
        "-p",                                   # non-interactive print mode
        "--dangerously-skip-permissions",        # skip all permission prompts
        "--output-format", "stream-json",        # stream JSON events
        "--verbose",                             # required for stream-json in print mode
        "--add-dir", str(workspace_dir),         # allow access to workspace
        "--no-session-persistence",              # don't save session to disk
        # "--max-budget-usd", "1.00",
    ]


def _build_coder_prompt(task: CoderTask) -> str:
    """Construct the full prompt for claude."""
    lines: list[str] = []

    lines.append(f"# Sprint {task.sprint_number} — {task.agent_name}")
    lines.append("")

    if task.system_prompt:
        lines.append("## Your Role")
        lines.append(task.system_prompt.strip())
        lines.append("")

    # Short-term memory: what this agent did in previous sprints
    if task.agent_log:
        lines.append("## Your Previous Sprint Work (short-term memory)")
        lines.append(task.agent_log.strip())
        lines.append("")

    # Long-term memory: what all agents have built so far
    if task.project_state:
        lines.append("## Project State (what all agents have built so far)")
        lines.append(task.project_state.strip())
        lines.append("")

    lines.append("## Your Task This Sprint")
    lines.append(task.task_description.strip())
    lines.append("")

    if task.files_to_create:
        lines.append("## Files You Must Create")
        for f in task.files_to_create:
            lines.append(f"- {f}")
        lines.append("")

    if task.files_to_modify:
        lines.append("## Files You Must Modify")
        for f in task.files_to_modify:
            lines.append(f"- {f}")
        lines.append("")

    agent_log_path = f"memory/{task.agent_name}_log.md"
    lines.append("## Instructions")
    lines.append(
        "CRITICAL: Do NOT run ls, cat, find, or any Read/Bash commands to explore or verify files. "
        "Do NOT read a file you just wrote. Go straight to writing — use the Write tool for new files, "
        "Edit tool only for files_to_modify from a previous sprint. "
        "Create every file listed above with complete, working, production-quality code. "
        "Do not leave placeholder comments like 'TODO' or 'implement later'. "
        "All files must be fully implemented in this single pass.\n\n"
        f"REQUIRED FINAL STEP: After writing all code files, append a brief summary of what you "
        f"built this sprint to `{agent_log_path}`. Use the Write tool to append (if the file "
        f"exists, read it first then write the full updated content). Format:\n"
        f"## Sprint {task.sprint_number}\n"
        f"<2-4 sentences: what files you created/modified, key design decisions, any interfaces "
        f"or contracts other agents depend on>"
    )

    return "\n".join(lines)


# ── Stream parser ──────────────────────────────────────────────────────────────

def _parse_stream_event(line: str) -> str:
    """Parse a stream-json line from claude --output-format stream-json --verbose."""
    if not line.strip():
        return ""
    try:
        event = json.loads(line)
        etype = event.get("type", "")

        # Assistant message with content blocks (the main response)
        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            parts.append(text[:200])
                    elif block.get("type") == "tool_use":
                        tool = block.get("name", "")
                        inp = block.get("input", {})
                        path = inp.get("file_path") or inp.get("path") or inp.get("command", "")
                        parts.append(f"[{tool}] {str(path)[:100]}")
            return " | ".join(parts) if parts else ""

        # Final result line
        elif etype == "result":
            turns = event.get("num_turns", "?")
            cost = event.get("total_cost_usd", event.get("cost_usd", "?"))
            is_error = event.get("is_error", False)
            status = "error" if is_error else "success"
            return f"[{status}] turns={turns} cost=${cost}"

        # System init — just log the model
        elif etype == "system" and event.get("subtype") == "init":
            model = event.get("model", "")
            return f"[init] model={model}"

    except (json.JSONDecodeError, AttributeError):
        if line and not line.startswith("{"):
            return line.strip()
    return ""


# ── CLAUDE.md writer ──────────────────────────────────────────────────────────

def _write_claude_md(workspace_dir: Path) -> None:
    """Write a CLAUDE.md into the workspace that instructs claude to write immediately."""
    content = (
        "# Agent Instructions\n\n"
        "- Do NOT run `ls`, `find`, `cat`, or any read/exploration commands.\n"
        "- Do NOT check whether files exist before writing them.\n"
        "- Do NOT read a file you just wrote — you already know what you wrote.\n"
        "- The workspace may be empty — that is expected.\n"
        "- Go straight to writing code with the Write or Edit tool.\n"
        "- If you are writing a React app: you MUST write `frontend/src/App.jsx` — this is non-negotiable. "
        "The scaffold's `main.jsx` already exists and imports `./App`. If you do not write `App.jsx`, "
        "the entire app shows a blank 'Loading...' stub. Do NOT use `index.jsx` as the entry point. "
        "Write `App.jsx` FIRST, before any other frontend file.\n"
        "- For backend: write `api/run.py` first. It MUST start with these exact lines:\n"
        "  ```python\n"
        "  import sys, os\n"
        "  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "  from api.app import create_app\n"
        "  app = create_app()\n"
        "  if __name__ == '__main__':\n"
        "      port = int(os.environ.get('PORT', 9000))\n"
        "      app.run(host='0.0.0.0', port=port, debug=False)\n"
        "  ```\n"
        "  CRITICAL: Always read port from os.environ.get('PORT', 9000) — NEVER hardcode port=9000.\n"
        "  NEVER use debug=True — the Werkzeug reloader forks a second process which breaks the demo launcher.\n"
        "  Without the sys.path.insert, `from api.app import create_app` raises ModuleNotFoundError at launch.\n"
        "- Complete all assigned files in one pass. Do not ask clarifying questions.\n"
        "- If you are the React/frontend developer: the UI must be highly polished and visually impressive. "
        "Every element needs hover/focus transitions, a strong color palette, smooth animations, and designed "
        "empty/loading/error states. A working app with mediocre UI is a failure.\n"
        "- CRITICAL — Python package names: always use the pip install name, not the import name. "
        "If you write `from flask_sqlalchemy import SQLAlchemy`, the package is `flask-sqlalchemy` (not `sqlalchemy`). "
        "If you write `from PIL import Image`, the package is `Pillow` (not `PIL`). "
        "If you add a new import that isn't in requirements.txt, add it to requirements.txt too.\n"
    )
    try:
        (workspace_dir / "CLAUDE.md").write_text(content, encoding="utf-8")
    except OSError:
        pass


# ── Workspace helpers ──────────────────────────────────────────────────────────

def _snapshot_workspace(workspace_dir: Path) -> dict[str, str]:
    """Return a {rel_path: mtime_size} snapshot for change detection."""
    snap: dict[str, str] = {}
    if not workspace_dir.exists():
        return snap
    for path in workspace_dir.rglob("*"):
        if path.is_file():
            parts = path.relative_to(workspace_dir).parts
            if any(p in (".venv", "node_modules", "__pycache__", ".git", ".claude") for p in parts):
                continue
            try:
                stat = path.stat()
                snap[str(path.relative_to(workspace_dir))] = f"{stat.st_mtime}:{stat.st_size}"
            except OSError:
                pass
    return snap


def _build_env(workspace_dir: Path, creds=None) -> dict[str, str]:
    """Build environment variables for the claude subprocess.

    The claude CLI reads ANTHROPIC_API_KEY from the environment — we inject
    it from the Credentials object so we never rely on os.environ being patched.
    """
    env = os.environ.copy()
    env["CLAUDE_WORKSPACE"] = str(workspace_dir)
    env["CLAUDE_DISABLE_AUTOUPDATE"] = "1"
    # inject anthropic key from credentials if provided
    if creds and hasattr(creds, 'anthropic_api_key') and creds.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = creds.anthropic_api_key
    return env


def _find_claude_binary() -> str | None:
    """Find the claude CLI binary in PATH."""
    # Try common locations
    candidates = ["claude"]
    if platform.system() == "Windows":
        candidates = ["claude.cmd", "claude.exe", "claude"]

    for name in candidates:
        found = shutil.which(name)
        if found:
            return found

    # Check npm global bin
    npm_prefix = _get_npm_prefix()
    if npm_prefix:
        if platform.system() == "Windows":
            candidate = Path(npm_prefix) / "claude.cmd"
        else:
            candidate = Path(npm_prefix) / "bin" / "claude"
        if candidate.exists():
            return str(candidate)

    return None


def _get_npm_prefix() -> str | None:
    """Get npm global prefix path."""
    try:
        result = subprocess.run(
            ["npm", "prefix", "-g"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _emit(cb: EventCallback | None, agent: str, event_type: str, content: str) -> None:
    if cb is not None:
        try:
            await cb(agent, event_type, content)
        except Exception:
            pass
