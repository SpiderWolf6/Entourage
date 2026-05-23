"""BaseAgent — async-only base class for all Entourage agents.

Each agent:
  - Has an async ``run(task, sprint, state)`` method.
  - Loads its system prompt from a file in ``prompts/<name>.txt``.
  - Uses the Context Builder to assemble its prompt context from PipelineState.
  - After running: applies output to state, writes a summary to MemoryStore,
    and appends a HistoryEntry.
"""

from __future__ import annotations

import os
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.pipeline_state import PipelineState

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")

# Pattern to extract MEMORY_ENTRY from LLM output (still used for summarization)
MEMORY_ENTRY_PATTERN = re.compile(
    r"^MEMORY_ENTRY:\s*\n(.*)",
    re.MULTILINE | re.DOTALL,
)


class BaseAgent:
    """Async-only base class for all Entourage agents.

    Subclasses declare ``name``, ``role``, ``skills``, and optionally override
    ``apply_output()``, ``build_prompt()``, and ``summarize()``.
    """

    name: str = "agent"
    role: str = "Agent"
    skills: list[str] = []

    # Inline system prompt — used as fallback if no prompt file exists.
    system_prompt: str = "You are a helpful software engineering assistant."

    def __init__(self):
        # Load system prompt from file if available
        prompt_path = os.path.join(PROMPTS_DIR, f"{self.name}.txt")
        if os.path.isfile(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.system_prompt = f.read()
        # Token tracking — populated after each run()
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0
        self.last_cost_usd: float = 0.0

    # ── Main entry point ──────────────────────────────────────────────

    async def run(
        self,
        task: dict[str, Any],
        sprint: dict[str, Any],
        state: "PipelineState",
    ) -> str:
        """Execute the agent on a single task within a sprint.

        Steps:
        1. Build context from PipelineState via Context Builder
        2. Build the final prompt (context + task)
        3. Call LLM
        4. Parse and apply output to state
        5. Write summary to MemoryStore and append HistoryEntry
        """
        from orchestrator.context_builder import build_context
        from llm.client import async_call_llm_tracked

        context = build_context(state, self.name, task, sprint)
        prompt = self.build_prompt(task, context)

        resp = await async_call_llm_tracked(
            self.system_prompt, prompt,
            max_tokens=self._max_tokens(),
            model=self._model_tier(),
            creds=getattr(state, 'creds', None),
        )
        raw_response = resp.text
        self.last_input_tokens = resp.input_tokens
        self.last_output_tokens = resp.output_tokens
        self.last_cost_usd = resp.cost_usd(self._model_tier())

        # Strip MEMORY_ENTRY section (legacy format) and use it as summary
        cleaned, memory_note = self._extract_memory_entry(raw_response)

        # Apply output to state (subclasses override for special behavior)
        files_modified = self.apply_output(cleaned, state)

        # Write summary to MemoryStore
        summary = memory_note or self.summarize(cleaned)
        state.memory_store.add(self.name, state.current_sprint, summary)

        # Write agent log artifact per architecture.md
        if state.project_path:
            from orchestrator.artifact_writer import append_agent_log
            append_agent_log(
                state.project_path, self.name, state.current_sprint, summary
            )

        # Append to history
        task_desc = task.get("task", task.get("description", ""))
        state.add_history(
            agent=self.name,
            task=task_desc,
            output_summary=summary,
            files_modified=files_modified or [],
        )

        return cleaned

    # ── Overridable methods ───────────────────────────────────────────

    def build_prompt(self, task: dict[str, Any], context: str) -> str:
        """Build the user prompt from task + context. Override for special input formats."""
        task_desc = task.get("description", task.get("task", ""))
        return f"{context}\n\n=== Your Task ===\n{task_desc}"

    def apply_output(self, output: str, state: "PipelineState") -> list[str]:
        """Apply agent output to state. Returns list of files modified.

        Default: parse FILES_TO_CREATE / FILES_TO_MODIFY blocks and update state.files.
        Override in subclasses with non-standard output formats.
        """
        from utils.parser import parse_dev_files

        created, modified = parse_dev_files(output)
        files_modified: list[str] = []

        for rel_path, content in created.items():
            state.update_file(rel_path, content)
            files_modified.append(rel_path)

        for rel_path, content in modified.items():
            state.update_file(rel_path, content)
            files_modified.append(rel_path)

        return files_modified

    def summarize(self, output: str) -> str:
        """Extract a short summary of the output for MemoryStore.

        Default: first 200 characters. Override for richer summaries.
        """
        # Take first non-empty line or first 200 chars
        clean = output.strip()
        if not clean:
            return "(no output)"
        first_line = clean.split("\n")[0].strip()
        if len(first_line) > 200:
            return first_line[:200] + "..."
        return first_line

    def _max_tokens(self) -> int:
        """Override in subclasses that need more tokens."""
        return 4096

    def _model_tier(self) -> str:
        """Override in subclasses to use a cheaper model.

        Default: "full" — all agents use GPT-4.1.
        """
        return "full"

    # ── Helpers ───────────────────────────────────────────────────────

    def _extract_memory_entry(self, raw_response: str) -> tuple[str, str]:
        """Extract MEMORY_ENTRY from LLM response.

        Returns (cleaned_response, memory_note). Memory note is empty string
        if no MEMORY_ENTRY found.
        """
        match = MEMORY_ENTRY_PATTERN.search(raw_response)
        if match:
            entry_text = match.group(1).strip()
            cleaned = raw_response[: match.start()].rstrip()
            return cleaned, entry_text
        return raw_response, ""
