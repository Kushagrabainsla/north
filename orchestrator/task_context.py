"""Task Context Object Store (Stage 4).

Uses a single shared SQLite database (tasks.db) with a task_id column instead
of one file per task.  This eliminates unbounded file accumulation and makes
cleanup a single DELETE statement rather than a filesystem scan.

See docs/CODING_STYLE.md Sections 5.2, 6.6, 9.7, 10.3, 11, 13.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

from orchestrator.exceptions import OrchestratorError
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
_SCHEMA_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_task_state_task_id ON task_state (task_id)"
)


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
            conn.commit()
        # Per-task condition variables: write() notifies, read() waits.
        self._conditions: dict[str, asyncio.Condition] = {}

    def _get_condition(self, task_id: str) -> asyncio.Condition:
        if task_id not in self._conditions:
            self._conditions[task_id] = asyncio.Condition()
        return self._conditions[task_id]

    async def initialize_task(self, task_id: str, agents: list[str]) -> None:
        """Insert pending status rows for every agent in this task."""
        def _run() -> None:
            now_str = format_timestamp(utcnow())
            with open_db_connection(self._db_path) as conn:
                for agent in agents:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO task_state
                            (task_id, agent, key, value, status, written_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (task_id, agent, "_status", None, "pending", now_str),
                    )
                conn.commit()

        await asyncio.to_thread(_run)

    async def read(
        self,
        task_id: str,
        requesting_agent: str,
        key: str,
        timeout: int = 30,
        required: bool = True,
    ) -> Any:
        """Read a key, waiting until the value is written or timeout expires."""
        if "." in key:
            source_agent, actual_key = key.split(".", 1)
        else:
            source_agent = requesting_agent
            actual_key = key

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        poll_interval = 2.0
        condition = self._get_condition(task_id)

        while True:
            def _check() -> sqlite3.Row | None:
                with open_db_connection(self._db_path) as conn:
                    try:
                        return conn.execute(
                            "SELECT value, status FROM task_state "
                            "WHERE task_id = ? AND agent = ? AND key = ?",
                            (task_id, source_agent, actual_key),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        return None

            row = await asyncio.to_thread(_check)
            if row:
                status = row["status"]
                if status == "completed":
                    val_str = row["value"]
                    if val_str is None:
                        return None
                    try:
                        return json.loads(val_str)
                    except json.JSONDecodeError:
                        return None
                elif status == "failed":
                    raise OrchestratorError(
                        f"Source agent '{source_agent}' failed. Cannot read '{key}'."
                    )

            elapsed = loop.time() - start_time
            if elapsed >= timeout:
                def _check_agent_status() -> str:
                    with open_db_connection(self._db_path) as conn:
                        try:
                            r = conn.execute(
                                "SELECT status FROM task_state "
                                "WHERE task_id = ? AND agent = ? AND key = ?",
                                (task_id, source_agent, "_status"),
                            ).fetchone()
                            return r["status"] if r else "unknown"
                        except sqlite3.OperationalError:
                            return "unknown"

                agent_status = await asyncio.to_thread(_check_agent_status)
                if agent_status in ("pending", "running"):
                    if required:
                        raise OrchestratorError(
                            f"Timeout: agent '{source_agent}' is still running but '{key}' is not ready."
                        )
                    return None
                elif agent_status == "failed":
                    raise OrchestratorError(
                        f"Agent '{source_agent}' failed. Key '{key}' is unavailable."
                    )
                else:
                    if required:
                        raise OrchestratorError(
                            f"Timeout: key '{key}' is missing (agent status: {agent_status})."
                        )
                    return None

            remaining = timeout - elapsed
            try:
                async with condition:
                    await asyncio.wait_for(
                        condition.wait(), timeout=min(remaining, poll_interval)
                    )
            except TimeoutError:
                pass

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
                conn.commit()

        await asyncio.to_thread(_run)
        condition = self._get_condition(task_id)
        async with condition:
            condition.notify_all()

    async def update_agent_status(self, task_id: str, agent: str, status: str) -> None:
        """Convenience: set an agent's run status."""
        await self.write(task_id, agent, "_status", None, status)

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

    def release_conditions(self, task_id: str) -> None:
        """Drop the in-memory Condition for a finished task. DB rows are preserved.

        Call this when a task completes so the conditions dict doesn't grow
        unboundedly. cleanup_stale_tasks() handles periodic DB pruning separately.
        """
        self._conditions.pop(task_id, None)

    async def delete_task(self, task_id: str) -> None:
        """Delete all rows belonging to a task_id."""
        def _run() -> None:
            with open_db_connection(self._db_path) as conn:
                conn.execute("DELETE FROM task_state WHERE task_id = ?", (task_id,))
                conn.commit()
        await asyncio.to_thread(_run)
        self._conditions.pop(task_id, None)

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
                conn.commit()
                return to_delete, result.rowcount

        deleted_ids, count = await asyncio.to_thread(_run)
        for task_id in deleted_ids:
            self._conditions.pop(task_id, None)
        return count
