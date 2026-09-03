"""Persistent storage for user-defined cron entries in the jobs SQLite DB.

A recurrence is stored as wall-clock `hour`/`minute`/`weekday` plus the IANA
`tz` they are read in - see `CronEntry` for why a repeating rule is not an
epoch. `created_epoch` is an instant, so it is one.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

from utils.db import open_db_connection
from utils.time import local_timezone_name, now_epoch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_cron_entries (
    name          TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    task          TEXT NOT NULL,
    hour          INTEGER NOT NULL,
    minute        INTEGER NOT NULL,
    weekday       INTEGER,
    tz            TEXT,
    created_epoch REAL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

# Columns added after v1; existing databases get them via ALTER (CODING_STYLE 11.4).
_ADDED_COLUMNS = {"tz": "TEXT", "created_epoch": "REAL"}

# User-created entries carry this prefix so a listing can tell them apart from
# the built-in schedules north ships with.
USER_PREFIX = "user_"
_SLUG_MAX_LENGTH = 40

# Fields a caller may change on an existing entry. `name` is the key, so
# renaming is a remove + add, not an update.
_UPDATABLE = ("agent", "task", "hour", "minute", "weekday", "tz")


def schedule_name(task: str) -> str:
    """Derive the stable key a schedule is addressed by, from its task text."""
    slug = re.sub(r"[^a-z0-9]+", "_", task.lower())[:_SLUG_MAX_LENGTH].strip("_")
    return USER_PREFIX + (slug or "schedule")


class UserCronStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(user_cron_entries)")}
            for column, decl in _ADDED_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE user_cron_entries ADD COLUMN {column} {decl}")
            # Entries written before zones were stored were read in the machine's
            # local zone by the old scheduler, so that is what they meant.
            conn.execute(
                "UPDATE user_cron_entries SET tz = ? WHERE tz IS NULL",
                (local_timezone_name(),),
            )

    async def add(
        self,
        name: str,
        agent: str,
        task: str,
        hour: int,
        minute: int,
        weekday: int | None,
        tz: str | None = None,
    ) -> None:
        await asyncio.to_thread(self._add_sync, name, agent, task, hour, minute, weekday, tz)

    def _add_sync(
        self,
        name: str,
        agent: str,
        task: str,
        hour: int,
        minute: int,
        weekday: int | None,
        tz: str | None,
    ) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_cron_entries
                    (name, agent, task, hour, minute, weekday, tz, created_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, agent, task, hour, minute, weekday, tz or local_timezone_name(), now_epoch()),
            )

    async def update(self, name: str, **fields: object) -> bool:
        """Change some fields of one entry. Returns False if no such entry exists.

        Unknown or None-valued fields are ignored, so a caller can pass through
        a request that only names what the user actually asked to change.
        """
        changes = {k: v for k, v in fields.items() if k in _UPDATABLE and v is not None}
        if not changes:
            return await self.get(name) is not None
        return await asyncio.to_thread(self._update_sync, name, changes)

    def _update_sync(self, name: str, changes: dict[str, object]) -> bool:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with open_db_connection(self._db_path) as conn:
            cursor = conn.execute(
                f"UPDATE user_cron_entries SET {assignments} WHERE name = ?",
                (*changes.values(), name),
            )
            return cursor.rowcount > 0

    async def remove(self, name: str) -> bool:
        """Delete one entry. Returns False if it was not there to delete."""
        return await asyncio.to_thread(self._remove_sync, name)

    def _remove_sync(self, name: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("DELETE FROM user_cron_entries WHERE name = ?", (name,)).rowcount > 0

    async def get(self, name: str) -> dict | None:
        row = await asyncio.to_thread(self._get_sync, name)
        return _row_to_entry(row) if row is not None else None

    def _get_sync(self, name: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("SELECT * FROM user_cron_entries WHERE name = ?", (name,)).fetchone()

    async def list(self) -> list[dict]:
        rows = await asyncio.to_thread(self._list_sync)
        return [_row_to_entry(r) for r in rows]

    def _list_sync(self) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(conn.execute("SELECT * FROM user_cron_entries ORDER BY created_at").fetchall())


def _row_to_entry(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "agent": row["agent"],
        "task": row["task"],
        "hour": row["hour"],
        "minute": row["minute"],
        "weekday": row["weekday"],
        "tz": row["tz"],
        "created_epoch": row["created_epoch"],
    }
