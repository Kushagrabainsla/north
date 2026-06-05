"""SQLite-backed implementation of LedgerWriter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from datetime import datetime
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

_INSERT_COLUMNS = (
    "id, timestamp, source, task_id, agent, input, action, output, "
    "agent_output, tools_used, model_used, tokens_in, tokens_out, cost_usd, status, "
    "duration_ms, error_type"
)
_INSERT_PLACEHOLDERS = ", ".join(["?"] * 17)


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
