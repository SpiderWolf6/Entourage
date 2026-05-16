"""Project Lead agent — plans sprints and reviews completed sprints."""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from .base import BaseAgent
from .registry import AgentRegistry

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState


# The review prompt is a separate system prompt used when reviewing sprints
REVIEW_PROMPT = """\
You are the Project Lead reviewing a completed sprint.

You receive the sprint plan with task results (statuses, notes, and test output from agents).

Your job:
1. Analyze what succeeded (DONE) and what failed (BLOCKED)
2. For BLOCKED dev tasks: add a retry/fix task in the next sprint with the specific error context in the description
3. For qa_dev tasks: the task notes contain [TEST OUTPUT] — the actual test runner output. Read it carefully:
   - PASSED tests: all good, no action needed for those
   - FAILED tests: identify which function/endpoint/component failed and why from the output
   - For each failure: add a fix task to the NEXT sprint for the responsible dev agent with the exact error in the description
   - Add a new qa_dev task at the end of that next sprint to re-run the failed tests (include exact test file paths)
   - DO NOT add qa_dev tasks for things not yet built — only test what has been written
4. You may sharpen future task descriptions based on what you learned from test failures

IMPORTANT: You can ONLY modify sprints AFTER the one just completed. Do NOT change the current or past sprints — their statuses and tasks are final. Do NOT renumber sprints or add new sprints between existing ones. Just modify future sprint tasks in place, or add tasks to existing future sprints.

Output EXACTLY:

REVIEW_SUMMARY:
<2-5 sentences: what worked, what didn't, adjustments made to future sprints>

UPDATED_SPRINT_PLAN:
```json
<full sprint plan JSON — past/current sprints exactly as received, future sprints may have modified task descriptions or added fix tasks>
```

MEMORY_ENTRY:
<Note about what you learned from this review.>

Rules:
- Valid parseable JSON
- Past and current sprint data MUST be identical to what you received (same statuses, same notes, same tasks)
- Only modify tasks in sprints numbered HIGHER than the sprint just completed
- Each sprint still has one task per available agent
- Keep the same sprint numbers — do NOT renumber, insert, or remove sprints
- Do NOT add other sections"""


@AgentRegistry.register("project_lead")
class ProjectLeadAgent(BaseAgent):
    name = "project_lead"
    role = "Project Lead"
    skills = ["sprint-planning", "task-assignment", "code-review", "project-management"]

    # The planning prompt is loaded from prompts/project_lead.txt via BaseAgent.__init__

    def _max_tokens(self) -> int:
        return 8192

    def _model_tier(self) -> str:
        return "mini"  # PL does planning/review, no code generation

    async def run(
        self,
        task: dict[str, Any],
        sprint: dict[str, Any],
        state: "PipelineState",
    ) -> str:
        """Project Lead has two modes: planning and review."""
        mode = task.get("mode", "plan")

        if mode == "review":
            return await self._run_review(task, sprint, state)
        else:
            return await self._run_plan(task, sprint, state)

    async def _run_plan(
        self,
        task: dict[str, Any],
        sprint: dict[str, Any],
        state: "PipelineState",
    ) -> str:
        """Plan sprints based on PO output and architecture."""
        from orchestrator.context_builder import build_context
        from llm import async_call_llm

        context = build_context(state, self.name, task, sprint)

        # Build planning prompt with dynamic agent list
        available_agents = task.get("available_agents", ["python_dev", "react_dev", "qa_dev"])
        agent_list = ", ".join(available_agents)
        system_prompt = (
            self.system_prompt
            + f"\n\nAVAILABLE AGENTS:\n{agent_list}\n\n"
            f"Only assign tasks to agents who have real work to do in that sprint. "
            f"Do NOT invent filler tasks. Variable task counts per sprint are fine."
        )

        # Get PO output and architecture from state
        po_output = ""
        arch_context = ""
        if state.architecture:
            po_output = state.architecture.get("po_output", "")
            arch_context = state.architecture.get("raw_output", "")

        # Inject stack info so PL can tell QA what test framework to use
        stack_info = ""
        if state.stack:
            from agents.stacks.profiles import get_profile, STACK_PROFILES
            if state.stack in STACK_PROFILES:
                p = get_profile(state.stack)
                stack_info = (
                    f"=== Project Stack ===\n"
                    f"Stack: {p.name}\n"
                    f"Backend: {p.backend_framework}\n"
                    f"Frontend: {p.frontend_framework}\n"
                    f"Test framework: {p.test_framework}\n"
                    f"Test command: {p.test_command}\n\n"
                )

        prompt = (
            f"{context}\n\n"
            f"=== Product Owner Requirements ===\n{po_output}\n\n"
            f"=== Architecture ===\n{arch_context}\n\n"
            f"{stack_info}"
            f"=== Project Path ===\n{state.project_path}\n\n"
            f"Create the sprint plan now."
        )

        raw = await async_call_llm(system_prompt, prompt,
                                   max_tokens=self._max_tokens(), model=self._model_tier())
        cleaned, memory_note = self._extract_memory_entry(raw)

        # Parse sprint plan from output
        files_modified = self._apply_plan_output(cleaned, state)

        summary = memory_note or self.summarize(cleaned)
        state.memory_store.add(self.name, state.current_sprint, summary)
        state.add_history(
            agent=self.name,
            task="Sprint planning",
            output_summary=summary,
            files_modified=files_modified,
        )

        return cleaned

    async def _run_review(
        self,
        task: dict[str, Any],
        sprint: dict[str, Any],
        state: "PipelineState",
    ) -> str:
        """Review a completed sprint and update future sprints."""
        from orchestrator.context_builder import build_context
        from llm import async_call_llm

        context = build_context(state, self.name, task, sprint)
        sprint_number = state.current_sprint + 1  # 1-indexed for display

        sprint_plan_json = json.dumps(
            {"total_sprints": len(state.sprint_plan or []), "sprints": state.sprint_plan or []},
            indent=2,
        )

        prompt = (
            f"{context}\n\n"
            f"=== Current Sprint Plan ===\n{sprint_plan_json}\n\n"
            f"=== Sprint Just Completed ===\nSprint {sprint_number}\n\n"
            f"Review this sprint and update the plan."
        )

        raw = await async_call_llm(REVIEW_PROMPT, prompt,
                                   max_tokens=self._max_tokens(), model=self._model_tier())
        cleaned, memory_note = self._extract_memory_entry(raw)

        # Parse updated sprint plan
        files_modified = self._apply_review_output(cleaned, state)

        summary = memory_note or f"Reviewed sprint {sprint_number}"
        state.memory_store.add(self.name, state.current_sprint, summary)
        state.add_history(
            agent=self.name,
            task=f"Sprint {sprint_number} review",
            output_summary=summary,
            files_modified=files_modified,
        )

        return cleaned

    def _apply_plan_output(self, output: str, state: "PipelineState") -> list[str]:
        """Parse sprint plan JSON from planning output and store in state."""
        from utils.parser import parse_sprint_plan
        from orchestrator.artifact_writer import write_sprint_plan

        sprint_plan = parse_sprint_plan(output)
        if sprint_plan and "sprints" in sprint_plan:
            state.sprint_plan = sprint_plan["sprints"]
            # Write sprint plan artifact
            if state.project_path:
                import json
                write_sprint_plan(state.project_path, json.dumps(sprint_plan, indent=2))
                return ["docs/sprint_plan.md"]
        return []

    def _apply_review_output(self, output: str, state: "PipelineState") -> list[str]:
        """Parse updated sprint plan from review output and store in state."""
        from utils.parser import parse_sprint_plan
        from orchestrator.artifact_writer import write_sprint_plan

        updated = parse_sprint_plan(output)
        if updated and isinstance(updated, dict) and "sprints" in updated:
            state.sprint_plan = updated["sprints"]
            # Update sprint plan artifact
            if state.project_path:
                import json
                write_sprint_plan(state.project_path, json.dumps(updated, indent=2))
                return ["docs/sprint_plan.md"]
        return []

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        """Not used directly — run() handles both modes."""
        return []
