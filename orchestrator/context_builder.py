"""Context Builder — assembles agent prompt context per architecture.md.

Injection order (per dev or QA agent call):
  1. design_doc_{agent}.md — full architecture for the agent's domain
  2. interface_contract.md — shared API contract
  3. sprint_plan.md — full plan, status, do-not-repeat list
  4. project_state.md — anti-drift registry
  5. {agent}_log.md — agent mid-term memory
  6. Task brief (current sprint) — specific task, acceptance criteria, constraints

Falls back to in-memory state when artifact files don't exist yet (backward compat).
"""

from __future__ import annotations

import re
from typing import Any

from orchestrator.pipeline_state import PipelineState, HistoryEntry


def build_context(
    state: PipelineState,
    agent_name: str,
    task: dict[str, Any],
    sprint: dict[str, Any],
) -> str:
    """Build the context string injected into an agent's prompt.

    Follows the 6-file injection order from architecture.md.
    """
    parts: list[str] = []

    # 1. Design doc for this agent's domain
    design_doc = _read_design_doc(state, agent_name)
    if design_doc:
        parts.append(f"=== Design Document ({agent_name}) ===\n{design_doc}")

    # 2. Interface contract — shared API contract
    interface_contract = _read_interface_contract(state)
    if interface_contract:
        parts.append(f"=== Interface Contract ===\n{interface_contract}")

    # 3. Sprint plan — full plan with statuses
    sprint_plan = _read_sprint_plan(state, sprint)
    if sprint_plan:
        parts.append(f"=== Sprint Plan ===\n{sprint_plan}")

    # 4. Project state — anti-drift registry
    project_state = _read_project_state(state)
    if project_state:
        parts.append(f"=== Project State ===\n{project_state}")

    # 5. Agent log — agent mid-term memory
    agent_log = _read_agent_log(state, agent_name)
    if agent_log:
        parts.append(f"=== Your Previous Work ===\n{agent_log}")

    # 6. Task brief + existing files (current sprint context)
    files_context = _build_files_section(state, agent_name, task)
    if files_context:
        parts.append(files_context)

    return "\n\n".join(parts)


# --- Section builders --------------------------------------------------------


def _read_design_doc(state: PipelineState, agent_name: str) -> str:
    """Read design doc from artifact file, falling back to state.architecture."""
    from orchestrator.artifact_writer import read_design_doc

    if state.project_path:
        # Try agent-specific design doc first
        content = read_design_doc(state.project_path, agent_name)
        if content:
            return content
        # Try generic architecture design doc
        content = read_design_doc(state.project_path, "architecture")
        if content:
            return content

    # Fallback: build from in-memory state.architecture
    if not state.architecture:
        return ""
    arch = state.architecture
    if isinstance(arch, dict):
        sections = []
        for key in ("ARCHITECTURE_MD", "DIRECTORY_STRUCTURE", "CODING_STANDARDS_MD"):
            content = arch.get(key, "")
            if content:
                label = key.replace("_", " ").title().replace(" Md", "")
                sections.append(f"--- {label} ---\n{content}")
        return "\n".join(sections)
    return str(arch)


def _read_interface_contract(state: PipelineState) -> str:
    """Read interface contract from artifact file."""
    from orchestrator.artifact_writer import read_interface_contract

    if state.project_path:
        content = read_interface_contract(state.project_path)
        if content:
            return content

    # Fallback: check state.architecture for INTERFACE_CONTRACT section
    if state.architecture and isinstance(state.architecture, dict):
        return state.architecture.get("INTERFACE_CONTRACT", "")
    return ""


def _read_sprint_plan(state: PipelineState, sprint: dict) -> str:
    """Read sprint plan from artifact file or build from state + current sprint."""
    from orchestrator.artifact_writer import read_sprint_plan as read_plan_file

    if state.project_path:
        content = read_plan_file(state.project_path)
        if content:
            # Also append current sprint status
            sprint_status = _build_current_sprint_status(state, sprint)
            if sprint_status:
                return f"{content}\n\n{sprint_status}"
            return content

    # Fallback: build from in-memory sprint plan
    return _build_current_sprint_status(state, sprint)


def _build_current_sprint_status(state: PipelineState, sprint: dict) -> str:
    """Build current sprint status section."""
    if not sprint:
        return ""
    parts = []
    sprint_name = sprint.get("sprint", sprint.get("name", f"Sprint {state.current_sprint + 1}"))
    parts.append(f"Current Sprint: {sprint_name}")
    tasks = sprint.get("tasks", [])
    if tasks:
        for t in tasks:
            status = t.get("status", "PENDING")
            agent = t.get("agent", t.get("role", "unknown"))
            desc = t.get("task", t.get("description", ""))
            parts.append(f"  [{status}] {agent}: {desc}")
    return "\n".join(parts)


def _read_project_state(state: PipelineState) -> str:
    """Read project_state.md from artifact file."""
    from orchestrator.artifact_writer import read_project_state

    if state.project_path:
        content = read_project_state(state.project_path)
        if content:
            return content
    return ""


def _read_agent_log(state: PipelineState, agent_name: str) -> str:
    """Read agent log from artifact file, falling back to in-memory history."""
    from orchestrator.artifact_writer import read_agent_log

    if state.project_path:
        content = read_agent_log(state.project_path, agent_name)
        if content:
            return content

    # Fallback: build from in-memory history
    agent_history = state.get_agent_history(agent_name)
    if agent_history:
        lines = [_format_history_entry(h) for h in agent_history[-10:]]
        return "\n".join(lines)
    return ""


def _build_files_section(
    state: PipelineState, agent_name: str, task: dict[str, Any]
) -> str:
    """Include relevant existing files for the agent's task."""
    if not state.files:
        return ""

    task_desc = task.get("description", task.get("task", ""))
    relevant_paths: set[str] = set()

    # Files mentioned in the task description
    for path in state.files:
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        if filename in task_desc or path in task_desc:
            relevant_paths.add(path)

    # Files this agent previously modified
    for entry in state.get_agent_history(agent_name):
        for fp in entry.files_modified:
            if fp in state.files:
                relevant_paths.add(fp)


    # If no specific files found, include key project files
    if not relevant_paths:
        key_files = _get_key_project_files(state)
        relevant_paths.update(key_files)

    if not relevant_paths:
        return ""

    sorted_paths = sorted(relevant_paths)[:15]

    parts = ["=== Existing Files ==="]
    for path in sorted_paths:
        content = state.files.get(path, "")
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated)"
        parts.append(f"--- FILE: {path} ---\n{content}\n--- END FILE ---")

    return "\n".join(parts)


def _get_key_project_files(state: PipelineState) -> list[str]:
    """Return a small set of key project files for general context."""
    key_patterns = [
        r"app\.py$", r"main\.py$", r"__init__\.py$",
        r"models\.py$", r"routes\.py$",
        r"App\.(tsx|jsx)$", r"index\.(tsx|jsx)$",
        r"package\.json$", r"requirements\.txt$",
        r"Makefile$", r"Cargo\.toml$",
    ]
    result = []
    for path in state.files:
        for pattern in key_patterns:
            if re.search(pattern, path):
                result.append(path)
                break
        if len(result) >= 8:
            break
    return result


def _format_history_entry(entry: HistoryEntry) -> str:
    """Format a single history entry for context display."""
    files_str = ""
    if entry.files_modified:
        files_str = f" (files: {', '.join(entry.files_modified[:5])})"
    return f"  [{entry.agent} | Sprint {entry.sprint}] {entry.output_summary}{files_str}"
