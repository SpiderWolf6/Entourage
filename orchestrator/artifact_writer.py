"""Artifact file read/write helpers per architecture.md.

All agent outputs are written to structured files in the project directory.
This module provides functions to write and read:
  - docs/requirements_doc.md
  - docs/specs_doc.md
  - docs/design_doc_{agent}.md
  - docs/interface_contract.md
  - docs/sprint_plan.md
  - memory/project_state.md
  - memory/{agent}_log.md
  - proposals/proposals.md
  - qa/qa_log_sprint_N.md
  - RUNBOOK.md
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def _write(project_path: str, rel_path: str, content: str) -> str:
    """Write content to a file in the project directory. Returns the absolute path."""
    abs_path = os.path.join(project_path, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


def _append(project_path: str, rel_path: str, content: str) -> str:
    """Append content to a file in the project directory. Creates if missing."""
    abs_path = os.path.join(project_path, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "a", encoding="utf-8") as f:
        f.write(content)
    return abs_path


def _read(project_path: str, rel_path: str) -> str:
    """Read a file from the project directory. Returns empty string if missing."""
    abs_path = os.path.join(project_path, rel_path)
    if not os.path.isfile(abs_path):
        return ""
    with open(abs_path, "r", encoding="utf-8") as f:
        return f.read()


# --- Docs artifacts ----------------------------------------------------------


def write_requirements_doc(project_path: str, content: str) -> str:
    return _write(project_path, "docs/requirements_doc.md", content)


def read_requirements_doc(project_path: str) -> str:
    return _read(project_path, "docs/requirements_doc.md")


def write_specs_doc(project_path: str, content: str) -> str:
    return _write(project_path, "docs/specs_doc.md", content)


def read_specs_doc(project_path: str) -> str:
    return _read(project_path, "docs/specs_doc.md")


def write_design_doc(project_path: str, agent_name: str, content: str) -> str:
    return _write(project_path, f"docs/design_doc_{agent_name}.md", content)


def read_design_doc(project_path: str, agent_name: str) -> str:
    return _read(project_path, f"docs/design_doc_{agent_name}.md")


def write_interface_contract(project_path: str, content: str) -> str:
    return _write(project_path, "docs/interface_contract.md", content)


def read_interface_contract(project_path: str) -> str:
    return _read(project_path, "docs/interface_contract.md")


def write_sprint_plan(project_path: str, content: str) -> str:
    return _write(project_path, "docs/sprint_plan.md", content)


def read_sprint_plan(project_path: str) -> str:
    return _read(project_path, "docs/sprint_plan.md")


# --- Memory artifacts --------------------------------------------------------


def write_project_state(project_path: str, content: str) -> str:
    """Overwrite the entire project_state.md."""
    return _write(project_path, "memory/project_state.md", content)


def read_project_state(project_path: str) -> str:
    return _read(project_path, "memory/project_state.md")


def append_project_state(project_path: str, additions: list[str]) -> str:
    """Append new entries to project_state.md after a sprint."""
    if not additions:
        return os.path.join(project_path, "memory/project_state.md")
    entry = "\n".join(f"- {a}" for a in additions) + "\n"
    return _append(project_path, "memory/project_state.md", entry)


def write_agent_log(project_path: str, agent_name: str, content: str) -> str:
    """Overwrite an agent's log file."""
    return _write(project_path, f"memory/{agent_name}_log.md", content)


def append_agent_log(
    project_path: str, agent_name: str, sprint: int, log_update: str
) -> str:
    """Append a sprint log entry to an agent's log file."""
    entry = f"\n## Sprint {sprint}\n{log_update}\n"
    return _append(project_path, f"memory/{agent_name}_log.md", entry)


def read_agent_log(project_path: str, agent_name: str) -> str:
    return _read(project_path, f"memory/{agent_name}_log.md")


# --- Proposals ---------------------------------------------------------------


def read_proposals(project_path: str) -> str:
    return _read(project_path, "proposals/proposals.md")


def append_proposal(
    project_path: str,
    sprint: int,
    agent_name: str,
    proposal_type: str,
    summary: str,
    impact: str = "",
    affects: str = "",
    action: str = "",
) -> str:
    """Append a proposal entry to proposals.md per architecture.md schema."""
    parts = [
        f"\n---\n[SPRINT {sprint} | {agent_name} | {proposal_type}]",
        "Status: OPEN",
        f"Summary: {summary}",
    ]
    if affects:
        parts.insert(2, f"Affects: {affects}")
    if impact:
        parts.append(f"Impact: {impact}")
    if action:
        parts.append(f"Action: {action}")
    entry = "\n".join(parts) + "\n"
    return _append(project_path, "proposals/proposals.md", entry)


# --- QA logs -----------------------------------------------------------------


def write_qa_log(project_path: str, sprint: int, content: str) -> str:
    return _write(project_path, f"qa/qa_log_sprint_{sprint}.md", content)


def read_qa_log(project_path: str, sprint: int) -> str:
    return _read(project_path, f"qa/qa_log_sprint_{sprint}.md")


# --- RUNBOOK -----------------------------------------------------------------


def write_runbook(project_path: str, content: str) -> str:
    return _write(project_path, "RUNBOOK.md", content)


def generate_runbook(
    project_path: str,
    project_type: str = "web_app",
    summary: str = "",
    run_instructions: str = "",
    launch_commands: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """Generate a RUNBOOK.md with concrete, stack-specific launch instructions."""
    # Build launch steps from the stack profile's launch_commands
    if launch_commands:
        steps = []
        for label, cmd in launch_commands:
            if "python" in cmd:
                steps.append(f"# Start {label}\ncd {project_path}\n.venv/Scripts/activate  # or source .venv/bin/activate on Mac/Linux\n{cmd}")
            elif cmd.startswith(("npm", "npx")):
                steps.append(f"# Start {label}\ncd {os.path.join(project_path, 'frontend')}\n{cmd}")
            else:
                steps.append(f"# Start {label}\ncd {project_path}\n{cmd}")
        launch_section = "\n\n".join(steps)
    elif run_instructions:
        launch_section = run_instructions
    else:
        launch_section = "See architecture/ directory for stack-specific instructions."

    content = f"""# Your project is ready!

## What was built
{summary}

## How to run

```bash
{launch_section}
```

## Project structure
- `api/` — Backend API server
- `frontend/` — Frontend application (if applicable)
- `tests/` — Test files
- `docs/` — Requirements, architecture, and design documents
- `memory/` — Project state and agent logs
"""
    return write_runbook(project_path, content)


# --- Infrastructure ----------------------------------------------------------


def write_requirements_txt(project_path: str, content: str) -> str:
    return _write(project_path, "infra/requirements.txt", content)


def write_dockerfile(project_path: str, content: str) -> str:
    return _write(project_path, "infra/Dockerfile", content)


# --- STATE_UPDATE helper (no LLM call) --------------------------------------


def run_state_update(
    project_path: str,
    sprint: int,
    state_additions: list[str],
) -> None:
    """STATE_UPDATE: scan new artifacts and append to project_state.md.

    This is a helper function (no LLM call) that runs after QA_RUN.
    Per architecture.md, it reads new code files and appends summaries
    to the project_state.md anti-drift registry.
    """
    header = f"\n## Sprint {sprint} additions\n"
    _append(project_path, "memory/project_state.md", header)
    append_project_state(project_path, state_additions)


# --- Directory scaffolding ---------------------------------------------------


def create_artifact_directories(project_path: str) -> None:
    """Create the full artifact directory structure per architecture.md."""
    dirs = [
        "docs",
        "memory",
        "proposals",
        "qa",
        "src",
        "src/frontend",
        "src/backend",
        "tests",
        "infra",
    ]
    for d in dirs:
        os.makedirs(os.path.join(project_path, d), exist_ok=True)

    # Initialize empty artifact files
    for rel_path in [
        "memory/project_state.md",
        "proposals/proposals.md",
    ]:
        abs_path = os.path.join(project_path, rel_path)
        if not os.path.isfile(abs_path):
            with open(abs_path, "w", encoding="utf-8") as f:
                if rel_path == "memory/project_state.md":
                    f.write("# Project State\n\n## Files created\n| File | Description |\n|------|-------------|\n\n")
                elif rel_path == "proposals/proposals.md":
                    f.write("# proposals.md\n")
