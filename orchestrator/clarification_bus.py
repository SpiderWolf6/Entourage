"""Clarification bus — lets the pipeline pause and wait for user answers.

Each project gets an asyncio.Event + a stored answer string. The pipeline
calls ``wait_for_answer(project_id)`` which blocks until the frontend POSTs
an answer via ``submit_answer(project_id, answer)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _Slot:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    answer: str = ""


class ClarificationBus:
    def __init__(self):
        self._slots: dict[str, _Slot] = {}

    def _slot(self, project_id: str) -> _Slot:
        if project_id not in self._slots:
            self._slots[project_id] = _Slot()
        return self._slots[project_id]

    async def wait_for_answer(self, project_id: str, timeout: float = 300.0) -> str:
        """Block until the user submits an answer (or timeout)."""
        slot = self._slot(project_id)
        slot.event.clear()
        slot.answer = ""
        try:
            await asyncio.wait_for(slot.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return ""
        return slot.answer

    def submit_answer(self, project_id: str, answer: str) -> None:
        """Called from the API endpoint when the user submits their answer."""
        slot = self._slot(project_id)
        slot.answer = answer
        slot.event.set()

    def cleanup(self, project_id: str) -> None:
        self._slots.pop(project_id, None)


clarification_bus = ClarificationBus()
