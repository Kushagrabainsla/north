"""Supervised fire-and-forget background tasks.

``spawn()`` is the single way north launches a background coroutine it will not
await. It keeps a strong reference to the task (asyncio only holds a weak one, so
an unreferenced task can be garbage-collected mid-flight) and attaches a
done-callback that logs any exception - a failing background task must never be
silent. Cancellation (e.g. on shutdown) is logged at debug, not as an error.

Use this instead of a bare ``asyncio.create_task`` for any task whose result is
not awaited.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

_logger = logging.getLogger("north.background")

# Strong references to in-flight tasks so they are not collected before they run.
_TASKS: set[asyncio.Task[Any]] = set()


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    logger: logging.Logger | None = None,
) -> asyncio.Task[Any]:
    """Launch *coro* as a supervised background task and return it.

    The task is retained until it finishes and any exception is logged under
    *name* (via *logger* or a default module logger), so failures surface instead
    of vanishing.
    """
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    log = logger or _logger

    def _on_done(finished: asyncio.Task[Any]) -> None:
        _TASKS.discard(finished)
        if finished.cancelled():
            log.debug("background task %r cancelled", name)
            return
        exc = finished.exception()
        if exc is not None:
            log.warning("background task %r failed: %s", name, exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def drain(timeout: float = 5.0) -> int:
    """Wait for the currently outstanding spawned tasks to finish.

    Fire-and-forget work is mostly durable writes (ledger entries, agent-run
    events), so on shutdown they need a chance to land before the process exits
    and connections close. Returns the number of tasks waited on. Never raises:
    a task that fails has already been logged by its own done-callback, and a
    task still running at *timeout* is left alone rather than cancelled.
    """
    pending = [task for task in _TASKS if not task.done()]
    if not pending:
        return 0
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        _logger.warning("drain: %d background task(s) still running after %.1fs", len(still_running), timeout)
    return len(done)
