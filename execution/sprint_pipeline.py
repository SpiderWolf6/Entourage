"""SprintExecutionPipeline — runs claude coders sprint-by-sprint.

Flow per sprint:
1. For each agent task in the sprint: launch ClaudeCoderAgent in the sandbox.
   Agents in the same sprint run in PARALLEL (each is independent).
2. Collect all file outputs, write to workspace.
3. Emit a "sprint_done" event to the EventBus.
4. Call ProjectLead.review to analyze what was done and update future sprints.
5. Emit "review_done" with updated sprint plan.
6. Gate: wait for project lead (user) to approve before moving to next sprint.
7. After all sprints: emit "mvp_ready", launch DemoLauncher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from execution.claude_coder import ClaudeCoderAgent, CoderTask, CoderResult
from execution.sandbox import SandboxManager

log = logging.getLogger(__name__)

# PL uses python_dev/react_dev; HR may spawn backend_dev/frontend_dev — normalize both
_ROLE_ALIASES: dict[str, list[str]] = {
    "python_dev":   ["backend_dev", "python_dev"],
    "react_dev":    ["frontend_dev", "react_dev"],
    "backend_dev":  ["backend_dev", "python_dev"],
    "frontend_dev": ["frontend_dev", "react_dev"],
    "qa_dev":       ["qa_dev"],
}


@dataclass
class SprintResult:
    sprint_number: int       # 1-indexed
    status: str              # "done" | "partial" | "failed"
    agent_results: list[CoderResult] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    review_summary: str = ""


@dataclass
class ExecutionCallbacks:
    """All event callbacks for the execution pipeline."""
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None


async def _emit(cb: ExecutionCallbacks, event_type: str, data: dict[str, Any]) -> None:
    if cb.on_event:
        try:
            await cb.on_event(event_type, data)
        except Exception as e:
            log.warning("Callback error on %s: %s", event_type, e)


class SprintExecutionPipeline:
    """Executes the sprint plan by running claude coders sprint by sprint.

    state: PipelineState (contains sprint_plan, architecture, spawned_agents)
    sandbox: SandboxManager for the project workspace
    """

    def __init__(
        self,
        project_id: str,
        state: Any,          # PipelineState
        sandbox: SandboxManager,
        callbacks: ExecutionCallbacks | None = None,
    ):
        self.project_id = project_id
        self.state = state
        self.sandbox = sandbox
        self.cb = callbacks or ExecutionCallbacks()
        self._coder = ClaudeCoderAgent(sandbox.workspace_dir, project_id)
        self._gate_events: dict[str, asyncio.Event] = {}
        self._gate_approved: dict[str, bool] = {}
        self._abort = asyncio.Event()
        self._sprint_results: list[SprintResult] = []

    # ── Public API ─────────────────────────────────────────────────────

    async def run(self) -> list[SprintResult]:
        """Run all sprints sequentially. Returns list of sprint results."""
        sprint_plan = self.state.sprint_plan or []
        if not sprint_plan:
            await _emit(self.cb, "execution_error", {"message": "No sprint plan found"})
            return []

        await _emit(self.cb, "execution_start", {
            "project_id": self.project_id,
            "total_sprints": len(sprint_plan),
        })

        # Setup sandbox first
        await self._setup_sandbox()

        results: list[SprintResult] = []

        for sprint_idx, sprint in enumerate(sprint_plan):
            if self._abort.is_set():
                break

            sprint_num = sprint_idx + 1
            await _emit(self.cb, "sprint_start", {
                "sprint": sprint_num,
                "total": len(sprint_plan),
                "name": sprint.get("name", f"Sprint {sprint_num}"),
            })

            sprint_result = await self._execute_sprint(sprint, sprint_num)
            results.append(sprint_result)
            self._sprint_results.append(sprint_result)

            # Update sprint status in state
            self.state.sprint_plan[sprint_idx]["status"] = (
                "done" if sprint_result.status == "done" else "partial"
            )

            await _emit(self.cb, "sprint_done", {
                "sprint": sprint_num,
                "status": sprint_result.status,
                "files_written": sprint_result.files_written,
            })

            # Project Lead review after each sprint
            if sprint_num < len(sprint_plan):
                review_summary = await self._run_pl_review(sprint_num)
                sprint_result.review_summary = review_summary

                await _emit(self.cb, "pl_review_done", {
                    "sprint": sprint_num,
                    "summary": review_summary,
                })

                # # Wait for gate approval (user confirms PL review before next sprint)
                # await _emit(self.cb, "awaiting_approval", {
                #     "sprint": sprint_num,
                #     "message": f"Sprint {sprint_num} complete. Review the Project Lead's notes and approve to continue.",
                # })
                # approved = await self._wait_for_gate(f"sprint_{sprint_num}")
                # if not approved:
                #     await _emit(self.cb, "execution_aborted", {
                #         "sprint": sprint_num,
                #         "reason": "Rejected by project lead",
                #     })
                #     break

        # All sprints done — launch demo
        await _emit(self.cb, "all_sprints_done", {
            "total": len(results),
            "files": self.sandbox.list_files(),
        })

        return results

    def approve_sprint(self, sprint_number: int) -> None:
        """Called from API when the project lead approves a sprint review."""
        key = f"sprint_{sprint_number}"
        self._gate_approved[key] = True
        if key in self._gate_events:
            self._gate_events[key].set()

    def reject_sprint(self, sprint_number: int, reason: str = "") -> None:
        """Called from API when the project lead rejects / requests changes."""
        key = f"sprint_{sprint_number}"
        self._gate_approved[key] = False
        if key in self._gate_events:
            self._gate_events[key].set()

    def abort(self) -> None:
        """Abort the pipeline immediately."""
        self._abort.set()
        for evt in self._gate_events.values():
            evt.set()

    # ── Sprint execution ───────────────────────────────────────────────

    async def _setup_sandbox(self) -> None:
        """Set up the project sandbox (venv, npm, initial files)."""
        arch = self.state.architecture or {}

        requirements_txt = (
            arch.get("REQUIREMENTS_TXT") or
            arch.get("requirements_txt") or
            _fallback_requirements(self.state.stack)
        )
        # Always ensure pytest is present for Python stacks — QA agent needs it
        if requirements_txt and "pytest" not in requirements_txt:
            requirements_txt = requirements_txt.rstrip() + "\npytest\n"
        package_json = (
            arch.get("PACKAGE_JSON") or
            arch.get("package_json") or
            _fallback_package_json(self.state.stack)
        )

        async def progress_cb(stage: str, msg: str):
            await _emit(self.cb, "sandbox_progress", {"stage": stage, "message": msg})

        await self.sandbox.setup(
            requirements_txt=requirements_txt,
            package_json=package_json,
            progress_cb=progress_cb,
        )

        # Write architecture files to workspace if present
        for key, filename in [
            ("ARCHITECTURE_MD", "docs/ARCHITECTURE.md"),
            ("CODING_STANDARDS_MD", "docs/CODING_STANDARDS.md"),
        ]:
            if arch.get(key):
                self.sandbox.write_file(filename, arch[key])

    async def _execute_sprint(self, sprint: dict, sprint_num: int) -> SprintResult:
        """Run all agent tasks in a sprint in parallel."""
        tasks_raw: list[dict] = sprint.get("tasks", [])
        if not tasks_raw:
            return SprintResult(sprint_number=sprint_num, status="done")

        spawned_agents = (self.state.architecture or {}).get("spawned_agents", [])
        # Index prompts by name AND archetype so we can match either
        agent_prompts: dict[str, str] = {}
        for a in spawned_agents:
            prompt = a.get("system_prompt", "")
            agent_prompts[a["name"]] = prompt
            if a.get("archetype"):
                agent_prompts[a["archetype"]] = prompt

        project_state = self.sandbox.read_project_state()

        # Build CoderTask for each task
        coder_tasks: list[CoderTask] = []
        for raw_task in tasks_raw:
            # Sprint plan may use "role" or "agent" key
            agent_name = raw_task.get("agent") or raw_task.get("role") or "dev"
            system_prompt = ""
            for alias in _ROLE_ALIASES.get(agent_name, [agent_name]):
                if alias in agent_prompts:
                    system_prompt = agent_prompts[alias]
                    break
            if not system_prompt:
                system_prompt = _default_system_prompt(agent_name)

            # Agent's own previous sprint log (short-term memory)
            agent_log = self.sandbox.read_agent_log(agent_name)

            coder_task = CoderTask(
                agent_name=agent_name,
                system_prompt=system_prompt,
                sprint_number=sprint_num,
                task_description=_build_task_description(raw_task),
                files_to_create=raw_task.get("files_to_create", []),
                files_to_modify=raw_task.get("files_to_modify", []),
                agent_log=agent_log,
                project_state=project_state,
            )
            coder_tasks.append(coder_task)

        # Split QA tasks from dev tasks — QA runs AFTER devs complete
        qa_tasks = [t for t in coder_tasks if t.agent_name == "qa_dev"]
        dev_tasks = [t for t in coder_tasks if t.agent_name != "qa_dev"]

        async def run_one(ct: CoderTask) -> CoderResult:
            await _emit(self.cb, "agent_start", {
                "agent": ct.agent_name,
                "sprint": sprint_num,
                "task": ct.task_description[:200],
            })

            async def on_coder_event(agent: str, event_type: str, content: str):
                await _emit(self.cb, "agent_stream", {
                    "agent": agent,
                    "sprint": sprint_num,
                    "event": event_type,
                    "content": content,
                })

            result = await self._coder.run(ct, on_event=on_coder_event)

            # Write memory: update agent log and project state after each agent
            if result.files_written:
                summary = _extract_agent_summary(result.output_text, result.files_written)
                self.sandbox.append_agent_log(ct.agent_name, sprint_num, summary)
                self.sandbox.update_project_state(sprint_num, ct.agent_name, result.files_written, summary)

            # Parse turns + cost from stream-json result line
            turns, cost = _parse_turns_cost(result.output_text)
            await _emit(self.cb, "agent_done", {
                "agent": ct.agent_name,
                "sprint": sprint_num,
                "status": result.status,
                "files": result.files_written,
                "turns": turns,
                "cost_usd": cost,
                "files_count": len(result.files_written),
            })
            return result

        # Run all dev tasks in parallel
        dev_results = list(await asyncio.gather(*[run_one(t) for t in dev_tasks]))

        # Run QA tasks after devs (QA needs their code to exist first)
        # Give QA an updated project_state reflecting dev work just done
        qa_results: list[CoderResult] = []
        if qa_tasks:
            updated_state = self.sandbox.read_project_state()
            for qa_task in qa_tasks:
                qa_task.project_state = updated_state
                qa_task.agent_log = self.sandbox.read_agent_log("qa_dev")
                qa_result = await run_one(qa_task)
                qa_results.append(qa_result)

            # Run tests and capture results
            if qa_results:
                test_output = await self._run_tests(sprint_num)
                if test_output:
                    # Store test output in QA agent's log and mark task notes
                    self.sandbox.append_agent_log("qa_dev", sprint_num, f"Test run output:\n{test_output[:800]}")
                    # Annotate last QA result with test output for PL review
                    qa_results[-1].output_text += f"\n\n[TEST OUTPUT]\n{test_output}"
                    await _emit(self.cb, "qa_tests_done", {
                        "sprint": sprint_num,
                        "output": test_output[:1000],
                    })

        agent_results_list = dev_results + qa_results
        all_files: list[str] = []
        for r in agent_results_list:
            all_files.extend(r.files_written)

        overall = "done" if all(r.status == "done" for r in agent_results_list) else "partial"

        return SprintResult(
            sprint_number=sprint_num,
            status=overall,
            agent_results=agent_results_list,
            files_written=list(set(all_files)),
        )

    # ── Test runner ────────────────────────────────────────────────────

    async def _run_tests(self, sprint_num: int) -> str:
        """Run the stack's test command in the workspace and return output."""
        try:
            from agents.stacks.profiles import get_profile
            profile = get_profile(self.state.stack or "flask_react")
            test_cmd = profile.test_command
            if not test_cmd:
                return ""

            await _emit(self.cb, "qa_tests_running", {
                "sprint": sprint_num,
                "command": test_cmd,
            })

            import shlex
            import subprocess as sp

            # Resolve python executable to venv if available
            cmd_parts = shlex.split(test_cmd)
            venv_dir = self.sandbox.workspace_dir / ".venv"
            if cmd_parts[0] in ("python", "python3"):
                if platform.system() == "Windows":
                    venv_py = venv_dir / "Scripts" / "python.exe"
                else:
                    venv_py = venv_dir / "bin" / "python"
                if venv_py.exists():
                    cmd_parts[0] = str(venv_py)
            elif cmd_parts[0] in ("npx", "npm"):
                if platform.system() == "Windows":
                    cmd_parts[0] = cmd_parts[0] + ".cmd"

            loop = asyncio.get_event_loop()

            def _run_blocking():
                env = os.environ.copy()
                if platform.system() == "Windows":
                    venv_bin = str(venv_dir / "Scripts")
                else:
                    venv_bin = str(venv_dir / "bin")
                env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
                env["PYTHONPATH"] = str(self.sandbox.workspace_dir) + os.pathsep + env.get("PYTHONPATH", "")
                return sp.run(
                    cmd_parts,
                    capture_output=True,
                    text=True,
                    cwd=str(self.sandbox.workspace_dir),
                    env=env,
                    timeout=120,
                )

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_blocking),
                timeout=130,
            )

            combined = ""
            if result.stdout:
                combined += result.stdout
            if result.stderr:
                combined += "\n[stderr]\n" + result.stderr
            combined = combined.strip()
            exit_label = "PASSED" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
            return f"[{exit_label}]\n{combined}"[:2000]

        except asyncio.TimeoutError:
            return "[TIMEOUT] Tests timed out after 120s"
        except Exception as e:
            log.warning("Test runner error sprint %d: %s", sprint_num, e)
            return f"[ERROR] Could not run tests: {e}"

    # ── PL review ─────────────────────────────────────────────────────

    async def _run_pl_review(self, sprint_just_done: int) -> str:
        """Run Project Lead review after a sprint completes."""
        await _emit(self.cb, "pl_review_start", {"sprint": sprint_just_done})

        try:
            from agents.project_lead import ProjectLeadAgent

            # Build task results for PL: what was done / blocked
            sprint_idx = sprint_just_done - 1
            sprint_data = self.state.sprint_plan[sprint_idx] if self.state.sprint_plan else {}

            # Inject file results + QA test output into sprint tasks
            _annotate_sprint_with_results(sprint_data, self.sandbox.list_files())
            _annotate_sprint_with_qa_output(sprint_data, self._sprint_results, sprint_just_done)

            pl_agent = ProjectLeadAgent()
            task = {
                "description": f"Review sprint {sprint_just_done}",
                "task": f"Review sprint {sprint_just_done}",
                "mode": "review",
            }
            self.state.current_sprint = sprint_idx

            output = await pl_agent.run(task, {}, self.state)

            # Parse review summary
            from utils.parser import parse_review_summary
            summary = parse_review_summary(output)
            return summary or f"Sprint {sprint_just_done} reviewed."

        except Exception as e:
            log.error("PL review failed for sprint %d: %s", sprint_just_done, e)
            return f"Review could not complete: {e}"

    # ── Gate management ────────────────────────────────────────────────

    async def _wait_for_gate(self, gate_key: str, timeout: float = 3600.0) -> bool:
        """Wait for a gate to be approved or rejected. Default timeout: 1 hour."""
        evt = asyncio.Event()
        self._gate_events[gate_key] = evt

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Auto-approve on timeout — don't block forever
            log.warning("Gate %s timed out — auto-approving", gate_key)
            return True
        finally:
            self._gate_events.pop(gate_key, None)

        return self._gate_approved.get(gate_key, True)


# ── Per-project execution registry ────────────────────────────────────────────

# Maps project_id → running SprintExecutionPipeline
_active_pipelines: dict[str, SprintExecutionPipeline] = {}


def register_pipeline(project_id: str, pipeline: SprintExecutionPipeline) -> None:
    _active_pipelines[project_id] = pipeline


def get_pipeline(project_id: str) -> SprintExecutionPipeline | None:
    return _active_pipelines.get(project_id)


def unregister_pipeline(project_id: str) -> None:
    _active_pipelines.pop(project_id, None)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_turns_cost(output_text: str) -> tuple[int, float]:
    """Extract turns and cost from claude stream-json output.
    Parses the 'result' JSON event line. Returns (turns, cost_usd)."""
    import json as _json
    import re
    turns, cost = 0, 0.0
    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Try JSON parsing first (stream-json format)
        if line.startswith('{'):
            try:
                obj = _json.loads(line)
                if obj.get("type") == "result":
                    turns = obj.get("num_turns", turns)
                    cost = float(obj.get("total_cost_usd", obj.get("cost_usd", cost)) or 0)
                    continue
            except Exception:
                pass
        # Fallback: regex on rendered text like "[success] turns=7 cost=$0.234"
        m = re.search(r"turns=(\d+)", line)
        if m:
            turns = int(m.group(1))
        m2 = re.search(r"cost=\$?([\d.]+)", line)
        if m2:
            cost = float(m2.group(1))
    return turns, cost


def _extract_agent_summary(output_text: str, files_written: list[str]) -> str:
    """Extract a readable summary from claude stream-json output.

    The stream contains JSON lines — we look for assistant text content
    in 'text' delta events and grab the first meaningful chunk.
    Falls back to listing files written if no text found.
    """
    import json as _json
    text_chunks = []
    for line in (output_text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        # stream-json text delta
        if obj.get("type") == "assistant" and isinstance(obj.get("message"), dict):
            for block in obj["message"].get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_chunks.append(block["text"])
        elif obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                text_chunks.append(delta.get("text", ""))

    text = "".join(text_chunks).strip()
    if text:
        # Return first 400 chars of the actual assistant response
        return text[:400]
    return f"Wrote {len(files_written)} file(s): {', '.join(files_written[:5])}"


def _build_task_description(raw_task: dict) -> str:
    """Build a human-readable task description from a sprint task dict."""
    lines = []
    if raw_task.get("description"):
        lines.append(raw_task["description"])
    if raw_task.get("done_when"):
        lines.append(f"\nDone when: {raw_task['done_when']}")
    if raw_task.get("notes"):
        lines.append(f"\nNotes: {raw_task['notes']}")
    return "\n".join(lines)


def _annotate_sprint_with_qa_output(sprint: dict, results: list["SprintResult"], sprint_num: int) -> None:
    """Inject QA test output into qa_dev task notes so PL sees the actual test results."""
    for sprint_result in results:
        if sprint_result.sprint_number != sprint_num:
            continue
        for agent_result in sprint_result.agent_results:
            if agent_result.agent_name != "qa_dev":
                continue
            if "[TEST OUTPUT]" not in agent_result.output_text:
                continue
            test_section = agent_result.output_text.split("[TEST OUTPUT]", 1)[-1].strip()
            for task in sprint.get("tasks", []):
                if task.get("role") == "qa_dev" or task.get("agent") == "qa_dev":
                    task["notes"] = (task.get("notes") or "") + f"\n[TEST OUTPUT]\n{test_section[:800]}"


def _annotate_sprint_with_results(sprint: dict, workspace_files: list[str]) -> None:
    """Annotate sprint tasks with completion status based on workspace files.

    Normalizes path separators before comparing — sprint plans use forward slashes
    but sandbox.list_files() returns OS-native paths (backslashes on Windows).
    """
    # Normalize workspace file list to forward slashes once
    normalized_workspace = {f.replace("\\", "/") for f in workspace_files}

    for task in sprint.get("tasks", []):
        expected = task.get("files_to_create", []) + task.get("files_to_modify", [])
        if not expected:
            task.setdefault("status", "done")
            continue
        created = [f for f in expected if f.replace("\\", "/") in normalized_workspace]
        if len(created) == len(expected):
            task["status"] = "done"
        elif created:
            task["status"] = "partial"
            task["notes"] = f"Created {len(created)}/{len(expected)} files"
        else:
            task["status"] = "blocked"
            task["notes"] = "No files were created"


def _fallback_requirements(stack: str) -> str:
    """Return a minimal requirements.txt when the architect forgot to output one."""
    return "flask\nflask-sqlalchemy\nflask-cors\npython-dotenv\npytest\n"


def _fallback_package_json(stack: str) -> str:
    """Return a minimal package.json when the architect forgot to output one."""
    import json
    pkg = {
        "name": "frontend",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
        "devDependencies": {
            "@vitejs/plugin-react": "^4.4.0",
            "@babel/core": "^7.0.0",
            "vite": "^4.4.0",
        },
    }
    return json.dumps(pkg, indent=2)


def _default_system_prompt(agent_name: str) -> str:
    """Fallback system prompt if HR didn't produce one for this agent."""
    role_map = {
        "python_dev":  "You are a senior Python backend developer.",
        "backend_dev": "You are a senior Python backend developer.",
        "react_dev":   "You are a senior React frontend developer.",
        "frontend_dev":"You are a senior React frontend developer.",
        "qa_dev":      "You are a senior QA engineer who writes tests.",
    }
    return role_map.get(agent_name, f"You are a senior software engineer ({agent_name}).")
