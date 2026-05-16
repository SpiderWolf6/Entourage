"""In-process event bus for real-time pipeline streaming.

Uses asyncio.Queue per subscriber. The pipeline publishes events,
and WebSocket handlers subscribe to receive them.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    """A pipeline event."""
    type: str           # phase_started, agent_output, file_created, sprint_completed, error, pipeline_done
    project_id: str
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Simple pub/sub using asyncio queues. One instance per application."""

    def __init__(self):
        # project_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        """Create a new subscription queue for a project."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(project_id, []).append(queue)
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscription queue."""
        subs = self._subscribers.get(project_id, [])
        if queue in subs:
            subs.remove(queue)
        if not subs:
            self._subscribers.pop(project_id, None)

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers for a project."""
        subs = self._subscribers.get(event.project_id, [])
        for queue in subs:
            await queue.put(event)


# Global singleton
event_bus = EventBus()
