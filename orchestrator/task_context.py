"""Task Context Object Store (Stage 4).

Shared scratch space for a task's agents: each agent writes its result here, and
the orchestrator reads them back to build handoff context, synthesise a final
answer, and reconstruct state when a run is retried.

Uses a single shared SQLite database (tasks.db) with a task_id column instead
of one file per task.  This eliminates unbounded file accumulation and makes
cleanup a single DELETE statement rather than a filesystem scan.

Writes only - there is no blocking read. Agents never wait on each other here:
the orchestrator sequences them and passes predecessors' output forward in the
next prompt (see `_execute_hierarchical_groups`), which is simpler to follow and
to debug than agents suspended on a condition variable.

See docs/CODING_STYLE.md Sections 5.2, 6.6, 9.7, 10.3, 11, 13.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

from utils.db import open_db_connection
from utils.time import format_timestamp, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_state (
    task_id     TEXT NOT NULL,
    agent       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    status      TEXT,
    written_at  DATETIME,
    PRIMARY KEY (task_id, agent, key)
)
"""
_SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS idx_task_state_task_id ON task_state (task_id)"


def _default_db_path() -> Path:
    from config.settings import settings  # deferred to avoid import cycle at module load

    tasks_dir = settings.north_home / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir / "tasks.db"


class TaskContextStore:
    """Single shared SQLite database for all in-flight task state.

    Each task's rows are namespaced by task_id.  This replaces the old
    one-file-per-task pattern, which created unbounded file accumulation.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path: Path = db_path if db_path is not None else _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_SCHEMA_INDEX)

    async def initialize_task(self, task_id: str, agents: list[str]) -> None:
        """Insert pending status rows for every agent in this task."""

        def _run() -> None:
            now_str = format_timestamp(utcnow())
            with open_db_connection(self._db_path) as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO task_state
                        (task_id, agent, key, value, status, written_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [(task_id, agent, "_status", None, "pending", now_str) for agent in agents],
                )

        await asyncio.to_thread(_run)

    async def write(
        self,
        task_id: str,
        agent: str,
        key: str,
        value: Any,
        status: str = "completed",
    ) -> None:
        """Write a key-value pair for an agent."""
        val_str = json.dumps(value) if value is not None else None
        now_str = format_timestamp(utcnow())

        def _run() -> None:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_state
                        (task_id, agent, key, value, status, written_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, agent, key, val_str, status, now_str),
                )

        await asyncio.to_thread(_run)

    async def update_agent_status(self, task_id: str, agent: str, status: str) -> None:
        """Convenience: set an agent's run status."""
        await self.write(task_id, agent, "_status", None, status)

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Convenience: set top-level task status."""
        await self.update_agent_status(task_id, "orchestrator", status)

    async def get_all(self, task_id: str) -> dict[str, dict[str, Any]]:
        """Return all completed key-value pairs (excluding _status) grouped by agent."""

        def _run() -> dict[str, dict[str, Any]]:
            with open_db_connection(self._db_path) as conn:
                try:
                    rows = conn.execute(
                        "SELECT agent, key, value FROM task_state "
                        "WHERE task_id = ? AND status = 'completed' AND key != '_status'",
                        (task_id,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return {}
            results: dict[str, dict[str, Any]] = {}
            for row in rows:
                agent = row["agent"]
                key = row["key"]
                try:
                    val = json.loads(row["value"]) if row["value"] is not None else None
                except json.JSONDecodeError:
                    val = None
                results.setdefault(agent, {})[key] = val
            return results

        return await asyncio.to_thread(_run)

    async def delete_task(self, task_id: str) -> None:
        """Delete all rows belonging to a task_id."""

        def _run() -> None:
            with open_db_connection(self._db_path) as conn:
                conn.execute("DELETE FROM task_state WHERE task_id = ?", (task_id,))

        await asyncio.to_thread(_run)

    async def cleanup_stale_tasks(
        self,
        active_task_ids: frozenset[str],
        completed_retention_days: int = 7,
        failed_retention_days: int = 30,
        # Legacy alias kept for callers that pass retention_days= by keyword.
        retention_days: int | None = None,
    ) -> int:
        """Delete rows for inactive tasks past their retention window.

        Failed tasks (any row with status='failed') are kept for
        failed_retention_days; all others for completed_retention_days.
        Conditions for deleted task_ids are pruned from the in-memory dict.
        Returns the number of rows removed.
        """
        if retention_days is not None:
            completed_retention_days = retention_days

        completed_cutoff = (utcnow() - datetime.timedelta(days=completed_retention_days)).isoformat()
        failed_cutoff = (utcnow() - datetime.timedelta(days=failed_retention_days)).isoformat()

        def _run() -> tuple[list[str], int]:
            with open_db_connection(self._db_path) as conn:
                if active_task_ids:
                    placeholders = ",".join("?" * len(active_task_ids))
                    candidate_rows = conn.execute(
                        f"""
                        SELECT task_id,
                               MAX(written_at) AS latest,
                               MAX(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS has_failure
                        FROM task_state
                        WHERE task_id NOT IN ({placeholders})
                        GROUP BY task_id
                        """,
                        tuple(active_task_ids),
                    ).fetchall()
                else:
                    candidate_rows = conn.execute(
                        """
                        SELECT task_id,
                               MAX(written_at) AS latest,
                               MAX(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS has_failure
                        FROM task_state
                        GROUP BY task_id
                        """
                    ).fetchall()

                to_delete = [
                    row["task_id"]
                    for row in candidate_rows
                    if row["latest"] < (failed_cutoff if row["has_failure"] else completed_cutoff)
                ]

                if not to_delete:
                    return [], 0

                del_placeholders = ",".join("?" * len(to_delete))
                result = conn.execute(
                    f"DELETE FROM task_state WHERE task_id IN ({del_placeholders})",
                    to_delete,
                )
                return to_delete, result.rowcount

        _deleted_ids, count = await asyncio.to_thread(_run)
        return count
