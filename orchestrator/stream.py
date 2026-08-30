"""Server-Sent Events stream manager.

Provides a lightweight, task-scoped publish/subscribe hub so the FastAPI
SSE endpoint can forward live progress to the browser.

See docs/CODING_STYLE.md Sections 6.7, 10, 12.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from typing import Any

from utils.execution_context import current_execution
from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)

# SSE message format (WHATWG)
_SSE_TEMPLATE = "event: {event}\ndata: {data}\n\n"

_HISTORY_MAX_EVENTS = 2048
_HISTORY_MAX_TASKS = 500
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_HEARTBEAT = ": keep-alive\n\n"

_DURABLE_EVENTS = frozenset({
    "agent_started",
    "agent_completed",
    "agent_failed",
    "tool_called",
    "tool_result",
    "skill_selected",
    "model",
    "model_response",
    "compaction",
    "stream_reset",
})


class EventStreamManager:
    """In-process pub/sub hub scoped to task_ids.

    Producers call ``emit()`` to push events; the SSE route calls
    ``subscribe()`` to get an async generator yielding raw SSE text.

    A bounded per-task replay buffer covers the gap between submitting a task
    and opening the SSE connection: late subscribers first receive the
    already-emitted events, and subscribing to a finished task replays its
    history and closes instead of blocking forever.
    """

    def __init__(self, run_store: Any | None = None) -> None:
        # Maps task_id → list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[str | None]]] = {}
        # Global subscribers receive every event (used by the TUI)
        self._global_subs: list[asyncio.Queue[str | None]] = []
        # task_id → recent SSE messages, oldest task evicted first
        self._history: OrderedDict[str, deque[str]] = OrderedDict()
        # task_ids whose stream has finished (emit_done was called)
        self._done: set[str] = set()
        # Bounded ring buffer of recently finished task_ids (survives eviction from _history)
        self._recently_finished: deque[str] = deque(maxlen=_HISTORY_MAX_TASKS * 2)
        self._run_store = run_store

    def _ensure_history(self, task_id: str) -> deque[str]:
        history = self._history.get(task_id)
        if history is not None:
            self._history.move_to_end(task_id)
            return history
        while len(self._history) >= _HISTORY_MAX_TASKS:
            evicted_id, _ = self._history.popitem(last=False)
            self._done.discard(evicted_id)
        history = deque(maxlen=_HISTORY_MAX_EVENTS)
        self._history[task_id] = history
        return history

    def _record_history(self, task_id: str, message: str) -> None:
        self._ensure_history(task_id).append(message)

    @property
    def tui_connected(self) -> bool:
        """True while at least one TUI session is subscribed to the global stream.

        Derived directly from the subscriber list so it can never get stuck
        True after an abrupt client disconnect.
        """
        return bool(self._global_subs)

    async def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """Publish an event to all subscribers for task_id.

        Args:
            task_id: The task this event belongs to.
            event:   SSE event name (e.g. "agent_started", "agent_completed").
            data:    Arbitrary JSON-serialisable payload.
        """
        # Reserved keys win over payload data so a data dict can never clobber
        # the routing fields subscribers rely on.
        execution = current_execution()
        run_id = data.get("run_id") or (execution.run_id if execution else None)
        parent_run_id = data.get("parent_run_id") or (execution.parent_run_id if execution else None)
        attempt = data.get("attempt") if "attempt" in data else (execution.attempt if execution else None)
        payload = {
            **data,
            "task_id": task_id,
            "event": event,
            "timestamp": format_timestamp(utcnow()),
        }
        if run_id:
            payload["run_id"] = run_id
        if parent_run_id:
            payload["parent_run_id"] = parent_run_id
        if attempt is not None:
            payload["attempt"] = attempt

        if self._run_store is not None and run_id and event in _DURABLE_EVENTS:
            await self._run_store.record_event(run_id, task_id, event, payload)
        message = _SSE_TEMPLATE.format(
            event=event,
            data=json.dumps(payload),
        )

        self._record_history(task_id, message)

        queues = list(self._subscribers.get(task_id, []))
        # Multiplex candidate events to the parent task subscriber queue
        if "__bo" in task_id:
            parent_id, _, cand_idx = task_id.partition("__bo")
            parent_queues = self._subscribers.get(parent_id, [])
            cand_payload = {**payload, "candidate_index": int(cand_idx) if cand_idx.isdigit() else cand_idx}
            cand_message = _SSE_TEMPLATE.format(event=event, data=json.dumps(cand_payload))
            for q in parent_queues:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(cand_message)

        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                if event != "token":
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(message)
                else:
                    logger.warning("SSE queue full for task %s - dropping event '%s'.", task_id, event)

        # Mirror every event to the global stream (TUI / watch clients).
        for q in self._global_subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(message)

    async def emit_done(self, task_id: str) -> None:
        """Signal the end of a task stream, causing subscribers to close.

        The None sentinel must always reach every subscriber - a subscriber
        that never receives it will block forever on queue.get().  If the
        queue is full, drain it first so the sentinel fits.
        """
        self._ensure_history(task_id)  # eviction of the history entry also clears the done flag
        self._done.add(task_id)
        self._recently_finished.append(task_id)
        queues = self._subscribers.get(task_id, [])
        for q in queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # Discard one oldest buffered message to make room, then deliver the sentinel.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)
                logger.warning(
                    "SSE queue was full for task %s; dropped oldest event to deliver done sentinel.", task_id
                )

    async def subscribe(self, task_id: str, max_queue_size: int = 2048) -> AsyncIterator[str]:
        """Async generator that yields raw SSE-formatted text for task_id.

        Replays buffered history first (events emitted before the client
        connected), then yields live events until a None sentinel is received
        (task done) or the caller disconnects.  Subscribing to a task that has
        already finished replays its history and closes immediately.

        Args:
            task_id:        The task to follow.
            max_queue_size: Maximum buffered events before drops occur.
        """
        # Snapshot before registering the queue - no await in between, so an
        # event is either in the snapshot or delivered via the queue, never both.
        replay = list(self._history.get(task_id, ()))
        done = (task_id in self._done) or (task_id in self._recently_finished)
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_queue_size)
        if not done:
            self._subscribers.setdefault(task_id, []).append(queue)
        try:
            for message in replay:
                yield message
            if done:
                return
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except TimeoutError:
                    # SSE comments keep proxies and browsers from expiring an
                    # otherwise healthy idle connection. EventSource ignores them.
                    yield _SSE_HEARTBEAT
                    continue
                if message is None:
                    return
                yield message
        finally:
            with contextlib.suppress(KeyError, ValueError):
                self._subscribers[task_id].remove(queue)
            if not self._subscribers.get(task_id):
                self._subscribers.pop(task_id, None)

    async def subscribe_global(self, max_queue_size: int = 2048) -> AsyncIterator[str]:
        """Async generator that yields all task events across the system.

        Stays open indefinitely - callers disconnect by cancelling the task or
        closing the HTTP connection.  The None sentinel is never sent here;
        the stream closes only when the caller disconnects.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_queue_size)
        self._global_subs.append(queue)
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _SSE_HEARTBEAT
                    continue
                if message is None:
                    return
                yield message
        finally:
            with contextlib.suppress(ValueError):
                self._global_subs.remove(queue)
