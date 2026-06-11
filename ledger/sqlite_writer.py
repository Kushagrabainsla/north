"""SQLite-backed implementation of LedgerWriter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ledger.base import LedgerFilters, LedgerWriter
from ledger.exceptions import LedgerReadError, LedgerWriteError
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from utils.db import open_db_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id              TEXT PRIMARY KEY,
    timestamp       DATETIME NOT NULL,
    source          TEXT NOT NULL,
    task_id         TEXT,
    agent           TEXT,
    input           TEXT,
    action          TEXT,
    output          TEXT,
    agent_output    JSON,
    tools_used      JSON,
    model_used      TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    status          TEXT,
    duration_ms     INTEGER,
    error_type      TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

# Columns added after the initial schema release; applied as migrations to
# existing databases so the writer works against both old and new files.
_MIGRATIONS = [
    "ALTER TABLE ledger ADD COLUMN duration_ms INTEGER",
    "ALTER TABLE ledger ADD COLUMN error_type TEXT",
]

# Column order for INSERTs. Placeholders are derived from this tuple so the two
# can never drift (CODING_STYLE §9.6 — no magic count).
_INSERT_COLUMN_NAMES = (
    "id", "timestamp", "source", "task_id", "agent", "input", "action", "output",
    "agent_output", "tools_used", "model_used", "tokens_in", "tokens_out", "cost_usd",
    "status", "duration_ms", "error_type",
)
_INSERT_COLUMNS = ", ".join(_INSERT_COLUMN_NAMES)
_INSERT_PLACEHOLDERS = ", ".join(["?"] * len(_INSERT_COLUMN_NAMES))


class SQLiteLedgerWriter(LedgerWriter):
    """Append-only ledger persisted to a single SQLite file.

    Initializes the schema on construction. Every public method off-loads
    blocking I/O to a thread so callers stay non-blocking on the event loop.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_task_id ON ledger (task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_timestamp ON ledger (timestamp DESC)")
            for migration in _MIGRATIONS:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(migration)

    async def write(self, entry: LedgerEntry) -> str:
        try:
            await asyncio.to_thread(self._write_sync, entry)
        except sqlite3.Error as e:
            raise LedgerWriteError(f"Failed to write entry {entry.id}: {e}") from e
        return entry.id

    def _write_sync(self, entry: LedgerEntry) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                f"INSERT INTO ledger ({_INSERT_COLUMNS}) VALUES ({_INSERT_PLACEHOLDERS})",
                (
                    entry.id,
                    entry.timestamp.isoformat(),
                    entry.source.value,
                    entry.task_id,
                    entry.agent,
                    entry.input,
                    entry.action,
                    entry.output,
                    json.dumps(entry.agent_output) if entry.agent_output is not None else None,
                    json.dumps(entry.tools_used),
                    entry.model_used,
                    entry.tokens_in,
                    entry.tokens_out,
                    entry.cost_usd,
                    entry.status.value if entry.status is not None else None,
                    entry.duration_ms,
                    entry.error_type,
                ),
            )

    async def get(self, entry_id: str) -> LedgerEntry | None:
        try:
            row = await asyncio.to_thread(self._get_sync, entry_id)
        except sqlite3.Error as e:
            raise LedgerReadError(f"Failed to read entry {entry_id}: {e}") from e
        return self._row_to_entry(row) if row is not None else None

    def _get_sync(self, entry_id: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()

    async def query(self, filters: LedgerFilters) -> list[LedgerEntry]:
        try:
            rows = await asyncio.to_thread(self._query_sync, filters)
        except sqlite3.Error as e:
            raise LedgerReadError(f"Failed to query ledger: {e}") from e
        return [self._row_to_entry(r) for r in rows]

    def _query_sync(self, filters: LedgerFilters) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list = []

        if filters.task_id is not None:
            clauses.append("task_id = ?")
            params.append(filters.task_id)
        if filters.agent is not None:
            clauses.append("agent = ?")
            params.append(filters.agent)
        if filters.source is not None:
            clauses.append("source = ?")
            params.append(filters.source.value)
        if filters.status is not None:
            clauses.append("status = ?")
            params.append(filters.status.value)
        if filters.since is not None:
            clauses.append("timestamp >= ?")
            params.append(filters.since.isoformat())

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        sql = f"SELECT * FROM ledger {where}ORDER BY timestamp DESC LIMIT ?"
        params.append(filters.limit)

        with open_db_connection(self._db_path) as conn:
            return list(conn.execute(sql, params).fetchall())

    async def pending_task_ids(self, limit: int = 500) -> list[str]:
        try:
            return await asyncio.to_thread(self._pending_task_ids_sync, limit)
        except sqlite3.Error as e:
            raise LedgerReadError(f"Failed to query pending task ids: {e}") from e

    def _pending_task_ids_sync(self, limit: int) -> list[str]:
        # Rank entries per task by recency and keep tasks whose newest entry is
        # still PENDING — completed/failed/cancelled tasks have a newer
        # terminal entry and drop out.
        sql = """
            SELECT task_id FROM (
                SELECT task_id,
                       status,
                       ROW_NUMBER() OVER (
                           PARTITION BY task_id
                           ORDER BY timestamp DESC, created_at DESC
                       ) AS rn
                FROM ledger
                WHERE task_id IS NOT NULL AND status IS NOT NULL
            )
            WHERE rn = 1 AND status = ?
            LIMIT ?
        """
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(sql, (LedgerStatus.PENDING.value, limit)).fetchall()
        return [r["task_id"] for r in rows]

    async def cost_breakdown(
        self,
        since: datetime,
        source: LedgerSource | None = None,
        agent: str | None = None,
    ) -> dict:
        try:
            return await asyncio.to_thread(self._cost_breakdown_sync, since, source, agent)
        except sqlite3.Error as e:
            raise LedgerReadError(f"Failed to aggregate costs: {e}") from e

    def _cost_breakdown_sync(self, since: datetime, source: LedgerSource | None, agent: str | None) -> dict:
        clauses = ["timestamp >= ?"]
        params: list = [since.isoformat()]
        if source is not None:
            clauses.append("source = ?")
            params.append(source.value)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        where = " AND ".join(clauses)

        with open_db_connection(self._db_path) as conn:
            total = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM ledger WHERE {where}",
                params,
            ).fetchone()["total"]
            component_rows = conn.execute(
                f"""SELECT COALESCE(agent, 'unknown') AS component,
                           COALESCE(SUM(cost_usd), 0.0) AS cost
                    FROM ledger WHERE {where} GROUP BY component""",
                params,
            ).fetchall()
            model_rows = conn.execute(
                f"""SELECT COALESCE(model_used, 'unknown') AS model,
                           COALESCE(SUM(cost_usd), 0.0) AS cost
                    FROM ledger WHERE {where} GROUP BY model""",
                params,
            ).fetchall()

        return {
            "total": total,
            "by_component": {r["component"]: r["cost"] for r in component_rows},
            "by_model": {r["model"]: r["cost"] for r in model_rows},
        }

    async def get_metrics(self, days: int = 7) -> dict:
        return await asyncio.to_thread(self._get_metrics_sync, days)

    def _get_metrics_sync(self, days: int) -> dict:
        since = datetime.now(UTC) - timedelta(days=days)
        since_iso = since.isoformat()

        with open_db_connection(self._db_path) as conn:
            totals = conn.execute(
                """SELECT COUNT(DISTINCT task_id) as total_tasks,
                          COALESCE(SUM(cost_usd), 0.0) as total_cost,
                          COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                          COALESCE(SUM(tokens_out), 0) as total_tokens_out
                   FROM ledger WHERE timestamp >= ? AND task_id IS NOT NULL""",
                (since_iso,),
            ).fetchone()

            agent_rows = conn.execute(
                """SELECT agent,
                          COUNT(DISTINCT task_id) as tasks,
                          COALESCE(SUM(cost_usd), 0.0) as cost_usd
                   FROM ledger
                   WHERE timestamp >= ? AND agent IS NOT NULL AND task_id IS NOT NULL
                   GROUP BY agent ORDER BY cost_usd DESC""",
                (since_iso,),
            ).fetchall()

            dur_rows = conn.execute(
                """SELECT agent, duration_ms FROM ledger
                   WHERE timestamp >= ? AND agent IS NOT NULL AND duration_ms IS NOT NULL
                   ORDER BY agent, duration_ms""",
                (since_iso,),
            ).fetchall()

            error_rows = conn.execute(
                """SELECT agent,
                          COUNT(DISTINCT task_id) as failed_tasks
                   FROM ledger
                   WHERE timestamp >= ? AND agent IS NOT NULL
                         AND task_id IS NOT NULL AND status = 'failed'
                   GROUP BY agent""",
                (since_iso,),
            ).fetchall()

            model_rows = conn.execute(
                """SELECT model_used, COALESCE(SUM(cost_usd), 0.0) as cost
                   FROM ledger
                   WHERE timestamp >= ? AND model_used IS NOT NULL
                   GROUP BY model_used ORDER BY cost DESC LIMIT 10""",
                (since_iso,),
            ).fetchall()

            top_error_rows = conn.execute(
                """SELECT error_type, COUNT(*) as cnt
                   FROM ledger
                   WHERE timestamp >= ? AND error_type IS NOT NULL
                   GROUP BY error_type ORDER BY cnt DESC LIMIT 10""",
                (since_iso,),
            ).fetchall()

        agent_durations: dict[str, list[int]] = defaultdict(list)
        for r in dur_rows:
            if r["agent"] and r["duration_ms"]:
                agent_durations[r["agent"]].append(r["duration_ms"])

        failed_by_agent: dict[str, int] = {r["agent"]: r["failed_tasks"] for r in error_rows}

        def _pct(vals: list[int], p: int) -> int | None:
            if not vals:
                return None
            idx = max(0, math.ceil(len(vals) * p / 100) - 1)
            return vals[idx]

        by_agent = []
        for r in agent_rows:
            agent = r["agent"]
            tasks = r["tasks"] or 0
            failed = failed_by_agent.get(agent, 0)
            durs = agent_durations.get(agent, [])
            by_agent.append(
                {
                    "agent": agent,
                    "tasks": tasks,
                    "success_rate": round((tasks - failed) / tasks, 3) if tasks > 0 else 0.0,
                    "cost_usd": round(r["cost_usd"] or 0.0, 6),
                    "p50_ms": _pct(durs, 50),
                    "p95_ms": _pct(durs, 95),
                }
            )

        return {
            "period_days": days,
            "total_tasks": totals["total_tasks"] or 0,
            "total_cost_usd": round(totals["total_cost"] or 0.0, 6),
            "total_tokens_in": totals["total_tokens_in"] or 0,
            "total_tokens_out": totals["total_tokens_out"] or 0,
            "by_agent": by_agent,
            "by_model": {r["model_used"]: round(r["cost"] or 0.0, 6) for r in model_rows},
            "top_errors": {r["error_type"]: r["cnt"] for r in top_error_rows},
        }

    async def prune(self, completed_before: datetime, failed_before: datetime) -> int:
        try:
            return await asyncio.to_thread(self._prune_sync, completed_before, failed_before)
        except sqlite3.Error as e:
            raise LedgerWriteError(f"Failed to prune ledger: {e}") from e

    def _prune_sync(self, completed_before: datetime, failed_before: datetime) -> int:
        # PENDING and CANCELLED rows share the completed retention window:
        # every task leaves an initial PENDING entry behind, so without this
        # they would accumulate forever.
        with open_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM ledger WHERE (status IN (?, ?, ?) AND timestamp < ?) OR (status = ? AND timestamp < ?)",
                (
                    LedgerStatus.COMPLETED.value,
                    LedgerStatus.PENDING.value,
                    LedgerStatus.CANCELLED.value,
                    completed_before.isoformat(),
                    LedgerStatus.FAILED.value,
                    failed_before.isoformat(),
                ),
            )
            return cur.rowcount

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            source=LedgerSource(row["source"]),
            task_id=row["task_id"],
            agent=row["agent"],
            input=row["input"],
            action=row["action"],
            output=row["output"],
            agent_output=json.loads(row["agent_output"]) if row["agent_output"] else None,
            tools_used=json.loads(row["tools_used"]) if row["tools_used"] else [],
            model_used=row["model_used"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=row["cost_usd"],
            status=LedgerStatus(row["status"]) if row["status"] else None,
            duration_ms=row["duration_ms"],
            error_type=row["error_type"],
        )
