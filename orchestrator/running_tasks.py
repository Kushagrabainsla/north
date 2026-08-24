"""Durable registry of in-flight tasks for crash recovery and the stuck watchdog.

A row exists exactly as long as a task is being processed: written when the task
starts (submit or resume), heartbeated as it makes progress, and deleted when it
reaches a terminal state. Rows that survive a restart are tasks that were
interrupted mid-flight - the reconciliation sweep resumes or fails them, and the
watchdog fails tasks whose heartbeat has gone stale.

Persisting the full request (prompt, source, workspace, context) also lets a
resumed task rebuild its exact ``TaskRequest`` - something the ledger, which only
stores the prompt, could not do.

The ``domain`` and ``description`` columns support live session awareness:
other agents can discover who else is running and what they're doing.
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
    has_side_effects INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'running',
    domain           TEXT NOT NULL DEFAULT 'general',
    description      TEXT NOT NULL DEFAULT ''
)
"""

# Applied idempotently for existing DBs.
_MIGRATION_ADD_SIDE_EFFECTS = "ALTER TABLE running_tasks ADD COLUMN has_side_effects INTEGER NOT NULL DEFAULT 0"
_MIGRATION_ADD_STATUS = "ALTER TABLE running_tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'running'"
_MIGRATION_ADD_DOMAIN = "ALTER TABLE running_tasks ADD COLUMN domain TEXT NOT NULL DEFAULT 'general'"
_MIGRATION_ADD_DESCRIPTION = "ALTER TABLE running_tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''"


@dataclass(frozen=True)
class RunningTask:
    """One interrupted-or-active task recovered from the registry."""

    task_id: str
    request: TaskRequest
    attempt: int
    started_at: datetime
    heartbeat_at: datetime
    has_side_effects: bool = False
    status: str = "running"  # "running" | "paused"
    domain: str = "general"
    description: str = ""


class RunningTaskStore:
    """SQLite-backed set of in-flight tasks. All methods are async (off-loop I/O)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            import contextlib

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_SIDE_EFFECTS)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_STATUS)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_DOMAIN)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_DESCRIPTION)

    async def mark_running(
        self, task_id: str, request: TaskRequest, *, attempt: int = 0, domain: str = "general"
    ) -> None:
        """Record (or refresh) a task as in-flight. ``started_at`` is set once."""
        await asyncio.to_thread(self._mark_running_sync, task_id, request, attempt, domain)

    def _mark_running_sync(self, task_id: str, request: TaskRequest, attempt: int, domain: str = "general") -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO running_tasks
                    (task_id, prompt, source, workspace, context, attempt, started_at, heartbeat_at, status, domain)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    prompt=excluded.prompt,
                    source=excluded.source,
                    workspace=excluded.workspace,
                    context=excluded.context,
                    attempt=excluded.attempt,
                    heartbeat_at=excluded.heartbeat_at,
                    status='running',
                    domain=excluded.domain
                """,
                (
                    task_id,
                    request.prompt,
                    request.source.value,
                    request.workspace,
                    request.context,
                    attempt,
                    now,
                    now,
                    domain,
                ),
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

    async def mark_paused(self, task_id: str) -> bool:
        """Mark a task as paused. Returns True if the task existed and was running."""
        return await asyncio.to_thread(self._mark_paused_sync, task_id)

    def _mark_paused_sync(self, task_id: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE running_tasks SET status = 'paused' WHERE task_id = ? AND status = 'running'",
                (task_id,),
            )
            return cur.rowcount > 0

    async def mark_running_from_paused(self, task_id: str) -> bool:
        """Transition a paused task back to running. Returns True if found and paused."""
        return await asyncio.to_thread(self._mark_running_from_paused_sync, task_id)

    def _mark_running_from_paused_sync(self, task_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE running_tasks SET status = 'running', heartbeat_at = ? WHERE task_id = ? AND status = 'paused'",
                (now, task_id),
            )
            return cur.rowcount > 0

    async def mark_queued(self, task_id: str, *, attempt: int = 0) -> bool:
        """Mark a task as queued waiting for model availability/capacity."""
        return await asyncio.to_thread(self._mark_queued_sync, task_id, attempt)

    def _mark_queued_sync(self, task_id: str, attempt: int) -> bool:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE running_tasks SET status = 'queued', attempt = ?, heartbeat_at = ? WHERE task_id = ?",
                (attempt, now, task_id),
            )
            return cur.rowcount > 0

    async def mark_running_from_queued(self, task_id: str) -> bool:
        """Transition a queued task back to running."""
        return await asyncio.to_thread(self._mark_running_from_queued_sync, task_id)

    def _mark_running_from_queued_sync(self, task_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE running_tasks SET status = 'running', heartbeat_at = ? WHERE task_id = ? AND status = 'queued'",
                (now, task_id),
            )
            return cur.rowcount > 0

    async def list_queued(self) -> list[RunningTask]:
        """Return all queued tasks waiting for execution, oldest first."""
        rows = await asyncio.to_thread(self._list_queued_sync)
        return [rt for rt in (self._row_to_task(r) for r in rows) if rt is not None]

    def _list_queued_sync(self) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(
                conn.execute("SELECT * FROM running_tasks WHERE status = 'queued' ORDER BY started_at ASC").fetchall()
            )

    async def is_queued(self, task_id: str) -> bool:
        """Return True if *task_id* is in the 'queued' state.

        Uses a targeted SELECT instead of fetching all queued rows.  This
        eliminates the O(n) full-table scan that ``list_queued()`` + linear
        search was doing for single-task checks.
        """
        return await asyncio.to_thread(self._is_queued_sync, task_id)

    def _is_queued_sync(self, task_id: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM running_tasks WHERE task_id = ? AND status = 'queued' LIMIT 1",
                (task_id,),
            ).fetchone()
            return row is not None

    async def clear(self, task_id: str) -> None:
        """Remove a task from the registry once it reaches a terminal state."""
        await asyncio.to_thread(self._clear_sync, task_id)

    def _clear_sync(self, task_id: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM running_tasks WHERE task_id = ?", (task_id,))

    async def update_domain(self, task_id: str, domain: str) -> None:
        """Set the domain for a running task (called after intent classification)."""
        await asyncio.to_thread(self._update_domain_sync, task_id, domain)

    def _update_domain_sync(self, task_id: str, domain: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE running_tasks SET domain = ? WHERE task_id = ?",
                (domain, task_id),
            )

    async def update_description(self, task_id: str, description: str) -> None:
        """Update the human-readable description for a running task."""
        await asyncio.to_thread(self._update_description_sync, task_id, description)

    def _update_description_sync(self, task_id: str, description: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE running_tasks SET description = ? WHERE task_id = ?",
                (description, task_id),
            )

    async def list_active(self, exclude_task_id: str | None = None) -> list[dict]:
        """Return dicts of currently running tasks, newest first, for agent awareness.

        Each dict contains: task_id, domain, description, started_at, heartbeat_at,
        and status. Pass exclude_task_id to omit the caller's own task.
        """

        def _run() -> list[dict]:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT task_id, domain, description, started_at, heartbeat_at, status "
                    "FROM running_tasks WHERE status = 'running' "
                    "ORDER BY started_at DESC"
                ).fetchall()
            result = []
            for r in rows:
                if exclude_task_id and r["task_id"] == exclude_task_id:
                    continue
                result.append(
                    {
                        "task_id": r["task_id"],
                        "domain": r["domain"],
                        "description": r["description"],
                        "started_at": r["started_at"],
                        "heartbeat_at": r["heartbeat_at"],
                        "status": r["status"],
                    }
                )
            return result

        return await asyncio.to_thread(_run)

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
        domain = str(row["domain"]) if "domain" in row else "general"
        description = str(row["description"]) if "description" in row else ""
        return RunningTask(
            task_id=row["task_id"],
            request=request,
            attempt=row["attempt"],
            started_at=datetime.fromisoformat(row["started_at"]),
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
            has_side_effects=bool(row["has_side_effects"]),
            status=str(row["status"]),
            domain=domain,
            description=description,
        )
