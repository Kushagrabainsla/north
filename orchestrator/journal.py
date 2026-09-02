"""Recording what happened on a task.

Every notable moment in a task has two audiences: the ledger, which keeps it
durably for later reasoning, and the live event stream, which shows it to
whoever is watching right now. They are always written together, so they are
written by one collaborator rather than two lines repeated at every call site.

Anything that needs to report on a task takes a ``TaskJournal`` instead of a
ledger *and* a stream manager - one direct dependency instead of two
(docs/CODING_STYLE.md Section 4.11).
"""

from __future__ import annotations

import logging
from typing import Any

from ledger.base import LedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.stream import EventStreamManager

logger = logging.getLogger(__name__)


class TaskJournal:
    """Writes a task's events to the ledger and the live stream."""

    def __init__(self, *, ledger: LedgerWriter, stream_manager: EventStreamManager) -> None:
        self._ledger = ledger
        self._stream_manager = stream_manager

    async def emit(self, task_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        """Send a live event to whoever is watching this task."""
        await self._stream_manager.emit(task_id, event, payload or {})

    async def stream_note(self, task_id: str, note: str) -> None:
        """Append text to the streamed answer.

        Used for after-the-fact notes (unmet Definition of Done, unverified
        claims). The answer has already been streamed token-by-token by the time
        these run, so a note that is only attached to the result would never
        reach the reader live.
        """
        await self.emit(task_id, "token", {"text": note})

    async def write(self, entry: LedgerEntry) -> None:
        """Ledger write with error logging; safe to fire-and-forget."""
        try:
            await self._ledger.write(entry)
        except Exception as exc:
            logger.error("Ledger write failed task=%s action=%s: %s", entry.task_id, entry.action, exc)

    async def record(
        self,
        task_id: str,
        action: str,
        *,
        status: LedgerStatus = LedgerStatus.COMPLETED,
        source: LedgerSource = LedgerSource.SYSTEM,
        agent: str | None = None,
        input: str = "",
        output: str = "",
        agent_output: dict[str, Any] | None = None,
        error_type: str | None = None,
        event: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record what happened: a durable ledger entry, then the live event.

        The keyword arguments mirror ``LedgerEntry``'s own fields, so this is the
        ledger write with the matching emit attached - the one thing a caller
        cannot forget to do. The defaults describe the common case: a completed
        system action.

        The event name defaults to the action, which is what makes a ledger
        history and a live stream tell the same story. Pass ``event`` only where
        the two genuinely differ.
        """
        await self.write(
            LedgerEntry.new(
                source=source,
                task_id=task_id,
                agent=agent,
                action=action,
                input=input,
                output=output,
                agent_output=agent_output,
                status=status,
                error_type=error_type,
            )
        )
        await self.emit(task_id, event or action, payload)
