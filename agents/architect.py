"""Architect agent — designs technical architecture from product requirements."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .base import BaseAgent
from .registry import AgentRegistry

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState


@AgentRegistry.register("architect")
class ArchitectAgent(BaseAgent):
    name = "architect"
    role = "Architect"
    skills = ["architecture", "system-design", "tech-stack", "api-design"]

    def _max_tokens(self) -> int:
        return 8192

    def build_prompt(self, task: dict[str, Any], context: str) -> str:
        """Architect receives PO output + available stack catalog to choose from."""
        from agents.stacks.profiles import get_profile, STACK_PROFILES

        # Get PO output from task or state
        po_output = task.get("description", task.get("task", ""))

        parts = [context]

        # Build a catalog of all available stacks for the architect to choose from
        catalog_lines = []
        for key, profile in STACK_PROFILES.items():
            catalog_lines.append(
                f"- **{key}**: {profile.name} — {profile.description}\n"
                f"  Backend: {profile.backend_framework}, Frontend: {profile.frontend_framework}\n"
                f"  Agents: {', '.join(profile.default_agents)}"
            )
        catalog = "\n".join(catalog_lines)

        parts.append(
            f"\n\n=== Available Technology Stacks ===\n"
            f"Choose the BEST stack for this project. State your choice in TECH_STACK as:\n"
            f"  STACK_PROFILE: <key>\n"
            f"(where <key> is one of the keys below)\n\n"
            f"{catalog}"
        )

        # Inject the pre-detected stack as a strong directive.
        # The architect must use it unless there is a clear, compelling reason not to,
        # in which case it must explicitly state why it is overriding.
        stack_key = task.get("stack", "")
        if stack_key and stack_key in STACK_PROFILES:
            profile = get_profile(stack_key)
            if profile and profile.architect_instructions:
                parts.append(
                    f"\n\n=== REQUIRED STACK: {stack_key} ({profile.name}) ===\n"
                    f"The stack classifier has selected this stack for this project. "
                    f"You MUST use it. In your TECH_STACK section, set STACK_PROFILE: {stack_key}.\n"
                    f"Only override this if you have a specific, explicit reason from the requirements "
                    f"(e.g., the user explicitly named a different technology). "
                    f"If you override, you MUST state: OVERRIDE_REASON: <your reason>.\n\n"
                    f"Stack-specific architecture instructions:\n{profile.architect_instructions}"
                )

        parts.append(f"\n\n=== Product Owner Requirements ===\n{po_output}")

        return "".join(parts)

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        """Parse architect output, store in state, and write artifact files."""
        import re
        from agents.stacks.profiles import STACK_PROFILES
        from utils.parser import parse_architect_output
        from orchestrator.artifact_writer import (
            write_design_doc,
            write_interface_contract,
            write_requirements_txt,
            write_dockerfile,
        )

        sections = parse_architect_output(output)

        # Extract stack choice from TECH_STACK section
        tech_stack = sections.get("TECH_STACK", "")
        stack_match = re.search(r"STACK_PROFILE:\s*(\S+)", tech_stack)
        if stack_match:
            chosen = stack_match.group(1).strip().lower()
            if chosen in STACK_PROFILES:
                state.stack = chosen

        # Store the parsed architecture sections (backward compat)
        if not state.architecture:
            state.architecture = {}
        state.architecture.update(sections)
        state.architecture["raw_output"] = output

        # Write artifact files per architecture.md
        files_written: list[str] = []
        if state.project_path:
            # Write per-agent design docs
            for key, content in sections.items():
                if key == "ARCHITECTURE_MD":
                    # This is the main architecture doc — write as a generic design doc
                    write_design_doc(state.project_path, "architecture", content)
                    files_written.append("docs/design_doc_architecture.md")
                elif key == "CODING_STANDARDS_MD":
                    write_design_doc(state.project_path, "coding_standards", content)
                    files_written.append("docs/design_doc_coding_standards.md")

            # Write interface contract if present in the architecture output
            # (extracted from the architecture sections or raw output)
            interface_content = sections.get("INTERFACE_CONTRACT", "")
            if interface_content:
                write_interface_contract(state.project_path, interface_content)
                files_written.append("docs/interface_contract.md")

            # Write infrastructure files
            req_content = sections.get("REQUIREMENTS_TXT", "")
            if req_content:
                write_requirements_txt(state.project_path, req_content)
                files_written.append("infra/requirements.txt")

            dockerfile_content = sections.get("DOCKERFILE", "")
            if dockerfile_content:
                write_dockerfile(state.project_path, dockerfile_content)
                files_written.append("infra/Dockerfile")

            # Write pubspec.yaml for Flutter projects
            pubspec_content = sections.get("PUBSPEC_YAML", "")
            if pubspec_content:
                import os
                pubspec_dir = os.path.join(state.project_path, "architecture")
                os.makedirs(pubspec_dir, exist_ok=True)
                pubspec_path = os.path.join(pubspec_dir, "pubspec.yaml")
                with open(pubspec_path, "w", encoding="utf-8") as f:
                    f.write(pubspec_content)
                files_written.append("architecture/pubspec.yaml")

        return files_written

    def summarize(self, output: str) -> str:
        """Summarize key architecture decisions."""
        import re
        match = re.search(r"TECH_STACK:\s*\n(.+?)(?=\n[A-Z_]+:|\Z)", output, re.DOTALL)
        if match:
            return f"Architecture: {match.group(1).strip()[:200]}"
        return output[:200]
