"""Durable registry of in-flight tasks for crash recovery and the stuck watchdog.

A row exists exactly as long as a task is being processed: written when the task
starts (submit or resume), heartbeated as it makes progress, and deleted when it
reaches a terminal state. Rows that survive a restart are tasks that were
interrupted mid-flight - the reconciliation sweep resumes or fails them, and the
watchdog fails tasks whose heartbeat has gone stale.

Persisting the full request (prompt, source, workspace, context) also lets a
resumed task rebuild its exact ``TaskRequest`` - something the ledger, which only
stores the prompt, could not do.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ledger.models import LedgerSource
from orchestrator.models import TaskRequest
from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS running_tasks (
    task_id          TEXT PRIMARY KEY,
    prompt           TEXT NOT NULL,
    source           TEXT NOT NULL,
    workspace        TEXT NOT NULL DEFAULT '',
    context          TEXT NOT NULL DEFAULT '',
    attempt          INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT NOT NULL,
    heartbeat_at     TEXT NOT NULL,
    has_side_effects INTEGER NOT NULL DEFAULT 0
)
"""

# Added after the initial release; applied idempotently for existing DBs.
_MIGRATION_ADD_SIDE_EFFECTS = "ALTER TABLE running_tasks ADD COLUMN has_side_effects INTEGER NOT NULL DEFAULT 0"


@dataclass(frozen=True)
class RunningTask:
    """One interrupted-or-active task recovered from the registry."""

    task_id: str
    request: TaskRequest
    attempt: int
    started_at: datetime
    heartbeat_at: datetime
    has_side_effects: bool = False


class RunningTaskStore:
    """SQLite-backed set of in-flight tasks. All methods are async (off-loop I/O)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            import contextlib

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_SIDE_EFFECTS)

    async def mark_running(self, task_id: str, request: TaskRequest, *, attempt: int = 0) -> None:
        """Record (or refresh) a task as in-flight. ``started_at`` is set once."""
        await asyncio.to_thread(self._mark_running_sync, task_id, request, attempt)

    def _mark_running_sync(self, task_id: str, request: TaskRequest, attempt: int) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO running_tasks
                    (task_id, prompt, source, workspace, context, attempt, started_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    prompt=excluded.prompt,
                    source=excluded.source,
                    workspace=excluded.workspace,
                    context=excluded.context,
                    attempt=excluded.attempt,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (task_id, request.prompt, request.source.value, request.workspace, request.context, attempt, now, now),
            )

    async def heartbeat(self, task_id: str) -> None:
        """Advance a task's heartbeat to now. No-op if the task is not registered."""
        await asyncio.to_thread(self._heartbeat_sync, task_id)

    def _heartbeat_sync(self, task_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute("UPDATE running_tasks SET heartbeat_at = ? WHERE task_id = ?", (now, task_id))

    async def mark_side_effect(self, task_id: str) -> None:
        """Flag that this task performed a mutating action (so it is not blindly re-run)."""
        await asyncio.to_thread(self._mark_side_effect_sync, task_id)

    def _mark_side_effect_sync(self, task_id: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("UPDATE running_tasks SET has_side_effects = 1 WHERE task_id = ?", (task_id,))

    async def clear(self, task_id: str) -> None:
        """Remove a task from the registry once it reaches a terminal state."""
        await asyncio.to_thread(self._clear_sync, task_id)

    def _clear_sync(self, task_id: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM running_tasks WHERE task_id = ?", (task_id,))

    async def list_all(self) -> list[RunningTask]:
        """Return every registered task, oldest first."""
        rows = await asyncio.to_thread(self._list_all_sync)
        return [rt for rt in (self._row_to_task(r) for r in rows) if rt is not None]

    def _list_all_sync(self) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(conn.execute("SELECT * FROM running_tasks ORDER BY started_at ASC").fetchall())

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> RunningTask | None:
        try:
            source = LedgerSource(row["source"])
        except ValueError:
            logger.warning(
                "running_tasks: unknown source %r for task %s; defaulting to prompt",
                row["source"],
                row["task_id"],
            )
            source = LedgerSource.PROMPT
        request = TaskRequest(
            prompt=row["prompt"],
            source=source,
            workspace=row["workspace"] or "",
            context=row["context"] or "",
        )
        return RunningTask(
            task_id=row["task_id"],
            request=request,
            attempt=row["attempt"],
            started_at=datetime.fromisoformat(row["started_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
            has_side_effects=bool(row["has_side_effects"]),
        )
