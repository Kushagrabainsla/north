"""Background watchdog that fails tasks whose heartbeat has gone stale.

A task heartbeats the ``RunningTaskStore`` as it makes progress. If a task stops
progressing - a wedged agent, a provider that never responds, an approval nobody
answers - its heartbeat stops advancing. This loop periodically scans the
registry and cancels any task whose heartbeat is older than the configured
threshold, so a single stuck task can never occupy a slot forever.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def watch_stuck_tasks(orchestrator: Orchestrator, *, poll_interval: int, max_age_seconds: int) -> None:
    """Run forever: every *poll_interval* seconds, cancel tasks stalled past the age."""
    store = orchestrator.running_task_store
    if store is None:
        logger.info("stuck-task watchdog disabled (no running-task store).")
        return
    while True:
        await asyncio.sleep(poll_interval)
        try:
            await _sweep(orchestrator, max_age_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stuck-task watchdog sweep failed")


async def _sweep(orchestrator: Orchestrator, max_age_seconds: int) -> None:
    store = orchestrator.running_task_store
    if store is None:
        return
    now = datetime.now(UTC)
    for rt in await store.list_all():
        # Paused tasks are intentionally idle, and queued tasks are waiting for capacity:
        # only actively running tasks are expected to heartbeat.
        if rt.status in ("paused", "queued"):
            continue
        stalled_for = (now - rt.heartbeat_at).total_seconds()
        if stalled_for <= max_age_seconds:
            continue
        logger.warning(
            "watchdog: task %s has not progressed for %.0fs (> %ds) - cancelling as stuck",
            rt.task_id,
            stalled_for,
            max_age_seconds,
        )
        await orchestrator.cancel_stuck_task(rt.task_id)
