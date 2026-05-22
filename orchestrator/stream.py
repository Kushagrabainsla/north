"""Server-Sent Events stream manager.

Provides a lightweight, task-scoped publish/subscribe hub so the FastAPI
SSE endpoint can forward live progress to the browser.

See docs/CODING_STYLE.md Sections 6.7, 10, 12.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)

# SSE message format (WHATWG)
_SSE_TEMPLATE = "event: {event}\ndata: {data}\n\n"


class EventStreamManager:
    """In-process pub/sub hub scoped to task_ids.

    Producers call ``emit()`` to push events; the SSE route calls
    ``subscribe()`` to get an async generator yielding raw SSE text.
    """

    def __init__(self) -> None:
        # Maps task_id → list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[str | None]]] = {}

    async def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """Publish an event to all subscribers for task_id.

        Args:
            task_id: The task this event belongs to.
            event:   SSE event name (e.g. "agent_started", "agent_completed").
            data:    Arbitrary JSON-serialisable payload.
        """
        payload = {
            "task_id": task_id,
            "event": event,
            "timestamp": format_timestamp(utcnow()),
            **data,
        }
        message = _SSE_TEMPLATE.format(
            event=event,
            data=json.dumps(payload),
        )

        queues = self._subscribers.get(task_id, [])
        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "SSE queue full for task %s — dropping event '%s'.", task_id, event
                )

    async def emit_done(self, task_id: str) -> None:
        """Signal the end of a task stream, causing subscribers to close."""
        queues = self._subscribers.get(task_id, [])
        for q in queues:
            try:
                q.put_nowait(None)  # None is the sentinel for "stream closed"
            except asyncio.QueueFull:
                pass

    async def subscribe(
        self, task_id: str, max_queue_size: int = 256
    ) -> AsyncIterator[str]:
        """Async generator that yields raw SSE-formatted text for task_id.

        Yields until a None sentinel is received (task done) or the caller
        disconnects.

        Args:
            task_id:        The task to follow.
            max_queue_size: Maximum buffered events before drops occur.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.setdefault(task_id, []).append(queue)
        try:
            while True:
                message = await queue.get()
                if message is None:
                    return
                yield message
        finally:
            try:
                self._subscribers[task_id].remove(queue)
            except (KeyError, ValueError):
                pass
            if not self._subscribers.get(task_id):
                self._subscribers.pop(task_id, None)
