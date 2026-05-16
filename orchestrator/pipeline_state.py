"""PipelineState — shared project state object used by all agents and the pipeline.

Planning-focused: stores artifacts, history, and memory without filesystem management.
The optional execution layer can extend this with workspace/file tracking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class HistoryEntry:
    """Structured record of a single agent action."""

    agent: str
    sprint: int
    task: str
    output_summary: str
    files_modified: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(**d)


@dataclass
class PipelineState:
    """Shared state object for a single planning run.

    Every agent reads from this; the pipeline and agents mutate it as work progresses.
    Persistence: ``save()`` / ``load()`` dump to JSON for resumability.
    """

    project_id: str
    project_path: str  # empty string for planning-only runs
    user_story: str
    stack: str  # stack profile key, e.g. "flask_react"

    # Populated by Architect (and PO output stored here too)
    architecture: dict[str, Any] | None = None

    # Populated by ProjectLead
    sprint_plan: list[dict[str, Any]] | None = None

    # Ground-truth file contents (used by execution layer, mostly empty for planning)
    files: dict[str, str] = field(default_factory=dict)

    # Ordered log of every agent action
    history: list[HistoryEntry] = field(default_factory=list)

    # Long-term knowledge store
    memory_store: Any = field(default=None)

    current_sprint: int = 0

    # FSM tracking
    fsm_phase: str = "INIT"
    completed_states: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.memory_store is None:
            from orchestrator.memory_store import MemoryStore
            self.memory_store = MemoryStore()

    # ── Persistence ───────────────────────────────────────────────────

    _STATE_FILENAME = "pipeline_state.json"

    def save(self, path: str | None = None) -> None:
        """Serialize state to JSON. Can write to a directory or be used in-memory."""
        data = self.to_dict()
        base = path or self.project_path
        if not base:
            return  # No path to save to (in-memory only run)
        fp = os.path.join(base, self._STATE_FILENAME)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def to_dict(self) -> dict:
        """Convert state to a serializable dict."""
        return {
            "project_id": self.project_id,
            "project_path": self.project_path,
            "user_story": self.user_story,
            "stack": self.stack,
            "architecture": self.architecture,
            "sprint_plan": self.sprint_plan,
            "files": self.files,
            "history": [h.to_dict() for h in self.history],
            "memory_store": self.memory_store.to_dict() if self.memory_store else {},
            "current_sprint": self.current_sprint,
            "phase": self.fsm_phase,
            "completed_states": self.completed_states,
        }

    @classmethod
    def load(cls, path: str) -> "PipelineState":
        """Deserialize state from a directory."""
        from orchestrator.memory_store import MemoryStore

        fp = os.path.join(path, cls._STATE_FILENAME)
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineState":
        """Create a PipelineState from a dict (e.g. from DB or JSON)."""
        from orchestrator.memory_store import MemoryStore

        history = [HistoryEntry.from_dict(h) for h in data.get("history", [])]
        memory_store = MemoryStore.from_dict(data.get("memory_store", {}))

        return cls(
            project_id=data["project_id"],
            project_path=data.get("project_path", ""),
            user_story=data["user_story"],
            stack=data["stack"],
            architecture=data.get("architecture"),
            sprint_plan=data.get("sprint_plan"),
            files=data.get("files", {}),
            history=history,
            memory_store=memory_store,
            current_sprint=data.get("current_sprint", 0),
            fsm_phase=data.get("phase", "INIT"),
            completed_states=data.get("completed_states", []),
        )

    # ── Helpers ───────────────────────────────────────────────────────

    def add_history(
        self,
        agent: str,
        task: str,
        output_summary: str,
        files_modified: list[str] | None = None,
    ) -> None:
        """Append a history entry for the current sprint."""
        self.history.append(HistoryEntry(
            agent=agent,
            sprint=self.current_sprint,
            task=task,
            output_summary=output_summary,
            files_modified=files_modified or [],
        ))

    def get_agent_history(self, agent_name: str) -> list[HistoryEntry]:
        """Return history entries for a specific agent."""
        return [h for h in self.history if h.agent == agent_name]

    def get_recent_history(self, n: int = 5) -> list[HistoryEntry]:
        """Return the last N history entries across all agents."""
        return self.history[-n:]

    def update_file(self, rel_path: str, content: str) -> None:
        """Update a file in state. Writes to disk only if project_path is set."""
        self.files[rel_path] = content
        if self.project_path:
            abs_path = os.path.join(self.project_path, rel_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

    def read_file(self, rel_path: str) -> str | None:
        """Read a file from state, falling back to disk if project_path is set."""
        if rel_path in self.files:
            return self.files[rel_path]
        if self.project_path:
            abs_path = os.path.join(self.project_path, rel_path)
            if os.path.isfile(abs_path):
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.files[rel_path] = content
                return content
        return None
