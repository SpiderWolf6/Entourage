"""hr director agent — last planning agent; reads the sprint plan and spawns dev personas.

the HR agent's output is a series of SPAWN_AGENT blocks, one per developer role.
each block contains: name, archetype (e.g. python_dev), and a system_prompt that
is injected into that developer's claude code session.

_parse_spawn_agents pulls the structured data out of the raw llm output so the
pipeline can emit agent_spawned events and the execution layer knows which agents to run.
"""

from __future__ import annotations
import re
from typing import Any, TYPE_CHECKING

from .base import BaseAgent
from .registry import AgentRegistry

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState


@AgentRegistry.register("hr")
class HRAgent(BaseAgent):
    name = "hr"
    role = "HR Director"
    skills = ["team-composition", "agent-spawning", "role-definition"]

    def _model_tier(self) -> str:
        return "full"

    def build_prompt(self, task: dict[str, Any], context: str) -> str:
        sprint_plan = task.get("sprint_plan", "")
        arch_summary = task.get("arch_summary", "")
        return (
            f"{context}\n\n"
            f"=== Architecture Summary ===\n{arch_summary}\n\n"
            f"=== Sprint Plan ===\n{sprint_plan}\n\n"
            "Based on the sprint plan above, determine which developer agents need to be "
            "spawned and write their system prompts."
        )

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        if not state.architecture:
            state.architecture = {}
        state.architecture["hr_output"] = output
        # parse and store spawned agent specs — used by execution layer to launch coders
        spawned = _parse_spawn_agents(output)
        state.architecture["spawned_agents"] = spawned
        return []

    def summarize(self, output: str) -> str:
        spawned = _parse_spawn_agents(output)
        names = [a["name"] for a in spawned]
        return f"spawned {len(names)} agents: {', '.join(names)}"


def _parse_spawn_agents(output: str) -> list[dict]:
    """extract SPAWN_AGENT blocks from HR output.

    the hr prompt instructs the llm to write one block per developer, separated by ---.
    each block has: SPAWN_AGENT:, name:, archetype:, system_prompt: | <yaml block scalar>.
    """
    agents = []
    # split on --- separator lines that hr uses to delimit agent blocks
    blocks = re.split(r"^---\s*$", output, flags=re.MULTILINE)
    for block in blocks:
        if "SPAWN_AGENT:" not in block:
            continue
        name_match = re.search(r"^name:\s*(.+)$", block, re.MULTILINE)
        archetype_match = re.search(r"^archetype:\s*(.+)$", block, re.MULTILINE)
        # system_prompt uses yaml block scalar (|) — capture everything until next key or end
        prompt_match = re.search(r"^system_prompt:\s*\|\s*\n(.*?)(?=\n\w|\Z)", block, re.DOTALL | re.MULTILINE)
        if name_match:
            agents.append({
                "name":          name_match.group(1).strip(),
                "archetype":     archetype_match.group(1).strip() if archetype_match else "unknown",
                "system_prompt": prompt_match.group(1).strip() if prompt_match else "",
            })
    return agents
