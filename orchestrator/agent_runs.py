"""Durable, queryable index of individual agent executions.

The ledger remains North's append-only audit trail.  This store provides the
missing execution hierarchy: one row per invocation, including delegated runs
and retries, plus a compact stream of significant events.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.db import open_db_connection
from utils.time import format_timestamp, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id          TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    parent_run_id   TEXT,
    agent           TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    prompt          TEXT NOT NULL DEFAULT '',
    workspace       TEXT NOT NULL DEFAULT '',
    model_pool      TEXT NOT NULL DEFAULT '',
    delegation_depth INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    duration_ms     INTEGER,
    output          TEXT,
    summary         TEXT,
    error           TEXT,
    models_used     TEXT NOT NULL DEFAULT '[]',
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    skills          TEXT NOT NULL DEFAULT '[]',
    provider_state  TEXT NOT NULL DEFAULT '{}'
)
"""

_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    timestamp   TEXT NOT NULL
)
"""

_SKILL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_usage (
    run_id          TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    skill_name      TEXT NOT NULL,
    skill_version   TEXT NOT NULL,
    skill_source    TEXT NOT NULL,
    outcome         TEXT,
    duration_ms     INTEGER,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    selected_at     TEXT NOT NULL,
    completed_at    TEXT,
    PRIMARY KEY (run_id, skill_name, skill_version)
)
"""

_MAX_EVENT_CHARS = 64_000


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    task_id: str
    parent_run_id: str | None
    agent: str
    attempt: int
    status: str
    prompt: str
    workspace: str
    model_pool: str
    delegation_depth: int
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    output: str | None
    summary: str | None
    error: str | None
    models_used: tuple[str, ...]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    skills: tuple[dict[str, str], ...]
    provider_state: dict[str, Any]


class AgentRunStore:
    """SQLite lifecycle index for agent invocations and significant events."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_EVENT_SCHEMA)
            conn.execute(_SKILL_USAGE_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id, started_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_run_events_run ON agent_run_events(run_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill_name, skill_version)")

    async def start(self, payload: Any, agent: str) -> None:
        await asyncio.to_thread(self._start_sync, payload, agent)

    def _start_sync(self, payload: Any, agent: str) -> None:
        now = format_timestamp(utcnow())
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_runs
                    (run_id, task_id, parent_run_id, agent, attempt, status, prompt,
                     workspace, model_pool, delegation_depth, started_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET status='running', error=NULL
                """,
                (
                    payload.run_id,
                    payload.task_id,
                    payload.parent_run_id,
                    agent,
                    payload.attempt,
                    payload.prompt,
                    payload.workspace,
                    payload.model_pool,
                    payload.delegation_depth,
                    now,
                ),
            )
    async def complete(self, run_id: str, result: Any) -> None:
        await asyncio.to_thread(self._complete_sync, run_id, result)

    def _complete_sync(self, run_id: str, result: Any) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE agent_runs SET status='completed', completed_at=?, duration_ms=?,
                    output=?, summary=?, models_used=?, tokens_in=?, tokens_out=?, cost_usd=?
                WHERE run_id=?
                """,
                (
                    format_timestamp(utcnow()),
                    result.duration_ms,
                    result.output,
                    result.summary,
                    json.dumps(result.models_used),
                    result.tokens_in,
                    result.tokens_out,
                    result.cost_usd,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE skill_usage SET outcome='completed', completed_at=?, duration_ms=?,
                    tokens_in=?, tokens_out=?, cost_usd=? WHERE run_id=?
                """,
                (
                    format_timestamp(utcnow()),
                    result.duration_ms,
                    result.tokens_in,
                    result.tokens_out,
                    result.cost_usd,
                    run_id,
                ),
            )

    async def finish_with_error(self, run_id: str, status: str, error: str) -> None:
        await asyncio.to_thread(self._finish_with_error_sync, run_id, status, error)

    def _finish_with_error_sync(self, run_id: str, status: str, error: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE agent_runs SET status=?, completed_at=?, error=? WHERE run_id=?",
                (status, format_timestamp(utcnow()), error[:8_000], run_id),
            )
            conn.execute(
                "UPDATE skill_usage SET outcome=?, completed_at=? WHERE run_id=?",
                (status, format_timestamp(utcnow()), run_id),
            )

    async def set_skills(self, run_id: str, skills: list[dict[str, str]]) -> None:
        await asyncio.to_thread(self._set_skills_sync, run_id, skills)

    def _set_skills_sync(self, run_id: str, skills: list[dict[str, str]]) -> None:
        now = format_timestamp(utcnow())
        with open_db_connection(self._db_path) as conn:
            row = conn.execute("SELECT task_id FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE agent_runs SET skills=? WHERE run_id=?", (json.dumps(skills), run_id))
            conn.executemany(
                """
                INSERT OR REPLACE INTO skill_usage
                    (run_id, task_id, skill_name, skill_version, skill_source, selected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["task_id"],
                        skill["name"],
                        skill["version"],
                        skill["source"],
                        now,
                    )
                    for skill in skills
                ],
            )

    async def merge_provider_state(self, run_id: str, state: dict[str, Any]) -> None:
        await asyncio.to_thread(self._merge_provider_state_sync, run_id, state)

    def _merge_provider_state_sync(self, run_id: str, state: dict[str, Any]) -> None:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute("SELECT provider_state FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            current = json.loads(row["provider_state"] or "{}") if row else {}
            provider = str(state.get("provider") or "unknown")
            entries = current.setdefault(provider, [])
            entries.append({k: v for k, v in state.items() if k != "provider"})
            conn.execute("UPDATE agent_runs SET provider_state=? WHERE run_id=?", (json.dumps(current), run_id))

    async def record_event(self, run_id: str, task_id: str, event: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._record_event_sync, run_id, task_id, event, data)

    def _record_event_sync(self, run_id: str, task_id: str, event: str, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, default=str)
        if len(encoded) > _MAX_EVENT_CHARS:
            encoded = json.dumps({"truncated": True, "preview": encoded[:_MAX_EVENT_CHARS]})
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO agent_run_events(run_id, task_id, event, data, timestamp) VALUES (?, ?, ?, ?, ?)",
                (run_id, task_id, event, encoded, format_timestamp(utcnow())),
            )

    async def get(self, run_id: str) -> AgentRun | None:
        row = await asyncio.to_thread(self._get_sync, run_id)
        return self._to_run(row) if row else None

    def _get_sync(self, run_id: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()

    async def list_for_task(self, task_id: str) -> list[AgentRun]:
        rows = await asyncio.to_thread(self._list_for_task_sync, task_id)
        return [self._to_run(row) for row in rows]

    def _list_for_task_sync(self, task_id: str) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(conn.execute("SELECT * FROM agent_runs WHERE task_id=? ORDER BY started_at", (task_id,)))

    async def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_events_sync, run_id)

    def _list_events_sync(self, run_id: str) -> list[dict[str, Any]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT event, data, timestamp FROM agent_run_events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            {"event": row["event"], "data": json.loads(row["data"]), "timestamp": row["timestamp"]}
            for row in rows
        ]

    @staticmethod
    def _to_run(row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            run_id=row["run_id"],
            task_id=row["task_id"],
            parent_run_id=row["parent_run_id"],
            agent=row["agent"],
            attempt=row["attempt"],
            status=row["status"],
            prompt=row["prompt"],
            workspace=row["workspace"],
            model_pool=row["model_pool"],
            delegation_depth=row["delegation_depth"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            duration_ms=row["duration_ms"],
            output=row["output"],
            summary=row["summary"],
            error=row["error"],
            models_used=tuple(json.loads(row["models_used"] or "[]")),
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=row["cost_usd"],
            skills=tuple(json.loads(row["skills"] or "[]")),
            provider_state=json.loads(row["provider_state"] or "{}"),
        )
