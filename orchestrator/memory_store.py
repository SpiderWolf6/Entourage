"""MemoryStore — simple retrieval system for agent output summaries.

Stores compressed knowledge (summaries of what each agent did) and retrieves
the most relevant entries for a given query using keyword matching.

Can be upgraded to embedding-based retrieval later via tools/rag.py ChromaDB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from collections import Counter
from typing import Any


@dataclass
class MemoryEntry:
    """A single memory: one agent's summary from one sprint."""

    agent: str
    sprint: int
    summary: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(**d)


# Simple tokenizer for keyword matching
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "and", "but", "or", "nor", "not", "so", "yet",
    "this", "that", "these", "those", "it", "its", "i", "we", "you",
    "he", "she", "they", "me", "him", "her", "us", "them", "my", "your",
    "his", "our", "their", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "only", "own", "same",
})


def _tokenize(text: str) -> list[str]:
    """Extract lowercase tokens, filtering stop words."""
    return [
        w.lower() for w in _WORD_RE.findall(text)
        if w.lower() not in _STOP_WORDS and len(w) > 1
    ]


class MemoryStore:
    """In-memory store of agent summaries with keyword-based retrieval."""

    def __init__(self):
        self.entries: list[MemoryEntry] = []

    def add(self, agent: str, sprint: int, summary: str) -> None:
        """Store a new memory entry."""
        self.entries.append(MemoryEntry(agent=agent, sprint=sprint, summary=summary))

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """Return the top-k most relevant entries for a query.

        Uses keyword overlap scoring — counts shared tokens between the query
        and each entry's summary. Ties broken by recency (later entries preferred).
        """
        if not self.entries:
            return []

        query_tokens = Counter(_tokenize(query))
        if not query_tokens:
            # No meaningful tokens in query — return most recent entries
            return self.entries[-top_k:]

        scored: list[tuple[float, int, MemoryEntry]] = []
        for idx, entry in enumerate(self.entries):
            entry_tokens = Counter(_tokenize(entry.summary))
            # Count overlapping tokens (intersection of counters)
            overlap = sum((query_tokens & entry_tokens).values())
            if overlap > 0:
                scored.append((overlap, idx, entry))

        # Sort by score desc, then by index desc (recency)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [entry for _, _, entry in scored[:top_k]]

    def get_agent_memories(self, agent_name: str) -> list[MemoryEntry]:
        """Return all memories for a specific agent."""
        return [e for e in self.entries if e.agent == agent_name]

    def get_sprint_memories(self, sprint: int) -> list[MemoryEntry]:
        """Return all memories from a specific sprint."""
        return [e for e in self.entries if e.sprint == sprint]

    # ── Persistence ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.to_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryStore":
        store = cls()
        store.entries = [MemoryEntry.from_dict(e) for e in d.get("entries", [])]
        return store
