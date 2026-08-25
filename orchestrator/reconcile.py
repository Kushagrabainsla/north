"""Startup recovery of tasks interrupted by a restart.

Every in-flight task is tracked in the ``RunningTaskStore`` for as long as it
runs; a clean finish removes its row. So any row still present at startup is a
task that was interrupted mid-flight (at *any* stage, not just intake). Each is
re-run under its original id via ``Orchestrator.resume_task`` unless it has
exhausted its resume budget or has been interrupted for longer than the stuck
threshold, in which case it is failed. Never raises - recovery must not block
startup.

See docs/CODING_STYLE.md Section 4.1 (single responsibility).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.constants import MAX_RESUME_ATTEMPTS

if TYPE_CHECKING:
    from orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def _fail_task(deps: Any, task_id: str, reason: str) -> None:
    """Mark an unrecoverable interrupted task FAILED and drop it from the registry."""
    await deps.ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_failed",
            output=reason,
            status=LedgerStatus.FAILED,
        )
    )
    if deps.running_task_store is not None:
        await deps.running_task_store.clear(task_id)


async def recover_interrupted_tasks(
    deps: Any, orchestrator: Orchestrator, *, max_age_seconds: int, resume_side_effecting: bool = False
) -> None:
    """Resume tasks interrupted by a restart; fail those that are too old or looping.

    A task that already performed a side effect (a mutating tool succeeded) is not
    auto-resumed unless *resume_side_effecting* is set, since re-running it could
    duplicate the action (a second git push, a second email); it is failed with a
    note so the user can re-submit deliberately.
    """
    try:
        store = deps.running_task_store
        interrupted = await store.list_all()
        if not interrupted:
            logger.info("Recovery: no interrupted tasks found.")
            return

        now = datetime.now(UTC)
        resumed: list[str] = []
        failed: list[str] = []
        for rt in interrupted:
            if rt.task_id in orchestrator.active_task_ids:
                continue  # already picked back up (shouldn't happen this early)
            # Paused tasks are user-initiated — only resume via explicit resume_paused_task().
            if rt.status == "paused":
                continue
            stalled_for = (now - rt.heartbeat_at).total_seconds()
            if rt.has_side_effects and not resume_side_effecting:
                await _fail_task(
                    deps,
                    rt.task_id,
                    "Interrupted after performing actions - not auto-resumed to avoid duplicating side "
                    "effects (e.g. a repeated push or message). Re-submit manually if you want it re-run.",
                )
                failed.append(rt.task_id)
            elif stalled_for > max_age_seconds:
                await _fail_task(
                    deps, rt.task_id, f"Interrupted task exceeded max age ({max_age_seconds}s) - marked failed."
                )
                failed.append(rt.task_id)
            elif rt.attempt >= MAX_RESUME_ATTEMPTS:
                await _fail_task(
                    deps,
                    rt.task_id,
                    f"Interrupted task exceeded {MAX_RESUME_ATTEMPTS} resume attempts - marked failed.",
                )
                failed.append(rt.task_id)
            elif await orchestrator.resume_task(rt.task_id, rt.request, attempt=rt.attempt + 1):
                resumed.append(rt.task_id)

        if resumed:
            logger.info("Recovery: resumed %d interrupted task(s): %s", len(resumed), resumed)
        if failed:
            logger.warning("Recovery: marked %d unrecoverable task(s) FAILED: %s", len(failed), failed)
    except Exception:
        logger.exception("Startup recovery sweep failed")
