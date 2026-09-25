"""Durable checkpoint storage for flow runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FlowRun:
    run_id: str
    flow_name: str
    task_id: str
    agent: str
    status: str
    current_step: int
    inputs: dict[str, Any]
    outputs: list[dict[str, Any]]
    flow_fingerprint: str = ""
    test_mode: bool = False
    error: str = ""
    updated_at: str = ""
    # When the run began, and what began it: "schedule", "manual" or "test".
    # Both are empty on runs recorded before history was shown anywhere.
    created_at: str = ""
    trigger: str = ""


class FlowRunStore:
    """SQLite-backed flow checkpoints.

    The store contains only structured step outputs and never executes code.
    Callers decide which values are safe for their flow to persist.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS flow_runs (
                    run_id TEXT PRIMARY KEY,
                    flow_name TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    inputs_json TEXT NOT NULL DEFAULT '{}',
                    outputs_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing = {row[1] for row in connection.execute("PRAGMA table_info(flow_runs)")}
            if "flow_fingerprint" not in existing:
                connection.execute("ALTER TABLE flow_runs ADD COLUMN flow_fingerprint TEXT NOT NULL DEFAULT ''")
            if "test_mode" not in existing:
                connection.execute("ALTER TABLE flow_runs ADD COLUMN test_mode INTEGER NOT NULL DEFAULT 0")
            if "created_at" not in existing:
                connection.execute("ALTER TABLE flow_runs ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            if "trigger" not in existing:
                connection.execute("ALTER TABLE flow_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT ''")

    def create(
        self,
        *,
        run_id: str,
        flow_name: str,
        task_id: str = "",
        agent: str = "",
        inputs: dict[str, Any] | None = None,
        flow_fingerprint: str = "",
        test_mode: bool = False,
        trigger: str = "",
    ) -> FlowRun:
        now = _now()
        run = FlowRun(
            run_id=run_id,
            flow_name=flow_name,
            task_id=task_id,
            agent=agent,
            status="running",
            current_step=0,
            inputs=inputs or {},
            outputs=[],
            flow_fingerprint=flow_fingerprint,
            test_mode=test_mode,
            updated_at=now,
            created_at=now,
            trigger=trigger,
        )
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO flow_runs "
                "(run_id, flow_name, task_id, agent, status, current_step, inputs_json, outputs_json, "
                "flow_fingerprint, test_mode, updated_at, created_at, trigger) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    flow_name,
                    task_id,
                    agent,
                    run.status,
                    0,
                    json.dumps(run.inputs),
                    "[]",
                    flow_fingerprint,
                    int(test_mode),
                    now,
                    now,
                    trigger,
                ),
            )
        return run

    def get(self, run_id: str) -> FlowRun | None:
        with sqlite3.connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM flow_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_runs(self, flow_name: str | None = None, limit: int = 50) -> list[FlowRun]:
        """Most recently started runs first, optionally for one flow.

        Ordered by start time, falling back to the last update for runs that
        predate the start time being recorded.
        """
        query = "SELECT * FROM flow_runs"
        params: list[Any] = []
        if flow_name is not None:
            query += " WHERE flow_name = ?"
            params.append(flow_name)
        query += " ORDER BY CASE WHEN created_at = '' THEN updated_at ELSE created_at END DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [_from_row(row) for row in rows]

    def prune(self, before: datetime) -> int:
        """Delete finished runs last touched before *before*; returns how many.

        A run that is still running or paused is never pruned - a paused run
        can be resumed, so its checkpoint is the only copy of its progress.
        """
        with sqlite3.connect(self._path) as connection:
            cursor = connection.execute(
                "DELETE FROM flow_runs WHERE updated_at < ? "
                "AND status IN ('completed', 'failed', 'rejected', 'cancelled')",
                (before.isoformat(),),
            )
            return cursor.rowcount

    def fail_interrupted(self) -> int:
        """Mark runs still recorded as running as failed; returns how many.

        Run at startup: nothing can be running before the process that would
        run it exists, so a "running" run was cut off by the last shutdown.
        Left alone it would read as in progress forever.
        """
        with sqlite3.connect(self._path) as connection:
            cursor = connection.execute(
                "UPDATE flow_runs SET status = 'failed', error = ? WHERE status = 'running'",
                ("North stopped while this run was in progress.",),
            )
            return cursor.rowcount

    def update(
        self,
        run_id: str,
        *,
        status: str,
        current_step: int,
        outputs: list[dict[str, Any]],
        error: str = "",
    ) -> FlowRun:
        now = _now()
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "UPDATE flow_runs SET status = ?, current_step = ?, outputs_json = ?, error = ?, updated_at = ? "
                "WHERE run_id = ?",
                (status, current_step, json.dumps(outputs), error, now, run_id),
            )
        run = self.get(run_id)
        if run is None:
            raise KeyError(f"Unknown flow run: {run_id}")
        return run


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _from_row(row: sqlite3.Row) -> FlowRun:
    return FlowRun(
        run_id=row["run_id"],
        flow_name=row["flow_name"],
        task_id=row["task_id"],
        agent=row["agent"],
        status=row["status"],
        current_step=row["current_step"],
        inputs=json.loads(row["inputs_json"]),
        outputs=json.loads(row["outputs_json"]),
        flow_fingerprint=row["flow_fingerprint"],
        test_mode=bool(row["test_mode"]),
        error=row["error"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
        trigger=row["trigger"],
    )
