"""engineering manager agent — first agent in the pipeline.

the EM does a short conversational intake (handled directly in planning_pipeline.py
_run_em, not here) to refine the user's idea into a structured brief.
this class mainly handles apply_output so the brief lands in state.architecture.
"""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

from .base import BaseAgent
from .registry import AgentRegistry

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState


@AgentRegistry.register("engineering_manager")
class EngineeringManagerAgent(BaseAgent):
    name = "engineering_manager"
    role = "Engineering Manager"
    skills = ["requirements-gathering", "scope-definition", "stakeholder-alignment"]

    def _model_tier(self) -> str:
        # mini is fast enough for the clarification conversation; saves cost
        return "mini"

    def build_prompt(self, task: dict[str, Any], context: str) -> str:
        conversation = task.get("conversation", "")
        user_story = task.get("description", task.get("task", ""))
        if conversation:
            return (
                f"{context}\n\n"
                f"=== Conversation so far ===\n{conversation}\n\n"
                f"=== Latest user message ===\n{user_story}"
            )
        return f"{context}\n\n=== User's initial idea ===\n{user_story}"

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        if not state.architecture:
            state.architecture = {}
        state.architecture["em_output"] = output
        # extract brief text so the PO gets a clean handoff rather than the full conversation
        if "BRIEF_READY:" in output:
            brief = output.split("BRIEF_READY:", 1)[1].strip()
            state.architecture["em_brief"] = brief
        return []

    def summarize(self, output: str) -> str:
        if "BRIEF_READY:" in output:
            brief = output.split("BRIEF_READY:", 1)[1].strip()
            return f"brief ready: {brief[:200]}"
        return output.strip()[:200]
