"""Product Owner agent — refines user stories into structured requirements."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .base import BaseAgent
from .registry import AgentRegistry

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState


@AgentRegistry.register("product_owner")
class ProductOwnerAgent(BaseAgent):
    name = "product_owner"
    role = "Product Owner"
    skills = ["requirements", "user-stories", "acceptance-criteria", "design-vision"]

    def _model_tier(self) -> str:
        return "mini"  # PO refines text, no code generation needed

    def build_prompt(self, task: dict[str, Any], context: str) -> str:
        """PO receives the raw user story as the task description."""
        user_story = task.get("description", task.get("task", ""))
        return f"{context}\n\n=== User Story ===\n{user_story}"

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        """Store the refined requirements in state and write specs_doc.md artifact."""
        from orchestrator.artifact_writer import write_specs_doc

        # Store raw PO output for the architect to consume (backward compat)
        if not state.architecture:
            state.architecture = {}
        state.architecture["po_output"] = output

        # Write specs artifact per architecture.md
        if state.project_path:
            write_specs_doc(state.project_path, output)

        return ["docs/specs_doc.md"]

    def summarize(self, output: str) -> str:
        """Extract the refined story as summary."""
        import re
        match = re.search(r"REFINED_USER_STORY:\s*\n(.+?)(?=\n[A-Z_]+:|\Z)", output, re.DOTALL)
        if match:
            return match.group(1).strip()[:300]
        return output[:200]
