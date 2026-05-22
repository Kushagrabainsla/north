"""Task Context Object Store (Stage 4).

See docs/CODING_STYLE.md Sections 5.2, 6.6, 9.7, 10.3, 11, 13.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from config.settings import settings
from orchestrator.exceptions import OrchestratorError
from utils.db import open_db_connection
from utils.time import format_timestamp, utcnow


class TaskContextStore:
    """SQLite-backed shared scratch space per task_id."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initializes the store.

        If db_path is provided, it dictates the exact SQLite file to use (dev/test).
        Otherwise, database paths are derived dynamically under ~/.north/tasks/.
        """
        self._db_path = db_path

    def _get_db_path(self, task_id: str) -> Path:
        if self._db_path is not None:
            return self._db_path
        tasks_dir = settings.north_home / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        return tasks_dir / f"task_{task_id}.db"

    def _init_db(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_state (
              agent         TEXT NOT NULL,
              key           TEXT NOT NULL,
              value         JSON,
              status        TEXT,
              written_at    DATETIME,
              PRIMARY KEY (agent, key)
            )
            """
        )

    async def initialize_task(self, task_id: str, agents: list[str]) -> None:
        """Sets up the SQLite file for the task and inserts pending statuses for agents."""
        db_path = self._get_db_path(task_id)

        def _run() -> None:
            with open_db_connection(db_path) as conn:
                self._init_db(conn)
                now_str = format_timestamp(utcnow())
                for agent in agents:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO task_state (agent, key, value, status, written_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (agent, "_status", None, "pending", now_str),
                    )

        await asyncio.to_thread(_run)

    async def read(
        self,
        task_id: str,
        requesting_agent: str,
        key: str,
        timeout: int = 30,
        required: bool = True,
    ) -> Any:
        """Reads a key, polling until the key is completed or timeout is reached."""
        if "." in key:
            source_agent, actual_key = key.split(".", 1)
        else:
            source_agent = requesting_agent
            actual_key = key

        db_path = self._get_db_path(task_id)
        start_time = asyncio.get_event_loop().time()
        poll_interval = 2.0

        while True:
            def _check() -> sqlite3.Row | None:
                if not db_path.exists():
                    return None
                with open_db_connection(db_path) as conn:
                    try:
                        cursor = conn.execute(
                            "SELECT value, status FROM task_state WHERE agent = ? AND key = ?",
                            (source_agent, actual_key),
                        )
                        return cursor.fetchone()
                    except sqlite3.OperationalError:
                        return None

            row = await asyncio.to_thread(_check)
            if row:
                status = row["status"]
                if status == "completed":
                    val_str = row["value"]
                    return json.loads(val_str) if val_str is not None else None
                elif status == "failed":
                    raise OrchestratorError(
                        f"Source agent '{source_agent}' failed. Cannot read '{key}'."
                    )

            # Check timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                def _check_agent_status() -> str:
                    if not db_path.exists():
                        return "unknown"
                    with open_db_connection(db_path) as conn:
                        try:
                            cursor = conn.execute(
                                "SELECT status FROM task_state WHERE agent = ? AND key = ?",
                                (source_agent, "_status"),
                            )
                            r = cursor.fetchone()
                            return r["status"] if r else "unknown"
                        except sqlite3.OperationalError:
                            return "unknown"

                agent_status = await asyncio.to_thread(_check_agent_status)
                if agent_status == "pending":
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
                            f"Timeout: key '{key}' is missing (source agent status: {agent_status})."
                        )
                    return None

            await asyncio.sleep(poll_interval)

    async def write(
        self,
        task_id: str,
        agent: str,
        key: str,
        value: Any,
        status: str = "completed",
    ) -> None:
        """Writes a key-value pair for an agent to the task database."""
        db_path = self._get_db_path(task_id)
        val_str = json.dumps(value) if value is not None else None
        now_str = format_timestamp(utcnow())

        def _run() -> None:
            with open_db_connection(db_path) as conn:
                self._init_db(conn)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_state (agent, key, value, status, written_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (agent, key, val_str, status, now_str),
                )

        await asyncio.to_thread(_run)

    async def update_agent_status(self, task_id: str, agent: str, status: str) -> None:
        """Convenience method to set an agent's run status in the task DB."""
        await self.write(task_id, agent, "_status", None, status)

    async def get_all(self, task_id: str) -> dict[str, dict[str, Any]]:
        """Retrieves all completed key-value pairs (excluding '_status') grouped by agent."""
        db_path = self._get_db_path(task_id)

        def _run() -> dict[str, dict[str, Any]]:
            if not db_path.exists():
                return {}
            with open_db_connection(db_path) as conn:
                try:
                    cursor = conn.execute(
                        "SELECT agent, key, value FROM task_state WHERE status = 'completed' AND key != '_status'"
                    )
                    results: dict[str, dict[str, Any]] = {}
                    for row in cursor.fetchall():
                        agent = row["agent"]
                        key = row["key"]
                        val_str = row["value"]
                        val = json.loads(val_str) if val_str is not None else None
                        if agent not in results:
                            results[agent] = {}
                        results[agent][key] = val
                    return results
                except sqlite3.OperationalError:
                    return {}

        return await asyncio.to_thread(_run)

    async def delete_task_file(self, task_id: str) -> None:
        """Deletes the task SQLite file if it exists."""
        db_path = self._get_db_path(task_id)

        def _run() -> None:
            if db_path.exists():
                db_path.unlink()

        await asyncio.to_thread(_run)
