"""Persistent storage for user-defined cron entries in the jobs SQLite DB.

A recurrence is stored as wall-clock `hour`/`minute`/`weekday` plus the IANA
`tz` they are read in - see `CronEntry` for why a repeating rule is not an
epoch. `created_epoch` is an instant, so it is one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Iterable
from itertools import count
from pathlib import Path
from typing import Any

from utils.db import open_db_connection
from utils.time import local_timezone_name, now_epoch

logger = logging.getLogger(__name__)

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
# `weekdays` supersedes the single-day `weekday`, which is left in place because
# SQLite cannot drop a column cheaply and a stale column costs nothing; it is
# read exactly once, by the migration below, and never written again.
_ADDED_COLUMNS = {
    "tz": "TEXT",
    "created_epoch": "REAL",
    "weekdays": "TEXT",
    "enabled": "INTEGER NOT NULL DEFAULT 1",
}

# User-created entries carry this prefix so a listing can tell them apart from
# the built-in schedules north ships with.
USER_PREFIX = "user_"
_SLUG_MAX_LENGTH = 40

# Fields a caller may change on an existing entry. `name` is the key, so
# renaming is a remove + add, not an update.
_UPDATABLE = ("agent", "task", "hour", "minute", "weekdays", "tz", "enabled")

# Distinguishes "the caller did not mention this field" from "the caller set it
# to nothing". Both arrive as None otherwise, which made it impossible to move a
# Tuesday schedule back to daily: the request to clear the day read as silence.
UNSET: Any = object()


def schedule_name(task: str) -> str:
    """Derive the stable key a schedule is addressed by, from its task text."""
    slug = re.sub(r"[^a-z0-9]+", "_", task.lower())[:_SLUG_MAX_LENGTH].strip("_")
    return USER_PREFIX + (slug or "schedule")


def encode_weekdays(weekdays: Iterable[int] | None) -> str | None:
    """Serialise a weekday set for storage. None (daily) stays NULL."""
    return None if weekdays is None else ",".join(str(day) for day in sorted(set(weekdays)))


def decode_weekdays(raw: object) -> frozenset[int] | None:
    """Read a stored weekday set back. Anything unparsable reads as daily.

    Tolerant on purpose: a row this cannot understand should still produce a
    schedule that runs, because a daily firing is a visible, fixable wrong -
    where raising here would take the whole scheduler down with one bad row.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, int):
        return frozenset({raw})
    try:
        return frozenset(int(part) for part in str(raw).split(",") if part.strip() != "")
    except ValueError:
        logger.warning("Unreadable weekdays %r - treating the schedule as daily", raw)
        return None


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
            # A single-day entry means the same rule as a one-element set, so it
            # migrates by being spelled the new way rather than by being rewritten.
            conn.execute(
                "UPDATE user_cron_entries SET weekdays = CAST(weekday AS TEXT)"
                " WHERE weekdays IS NULL AND weekday IS NOT NULL"
            )

    async def add(
        self,
        name: str,
        agent: str,
        task: str,
        hour: int,
        minute: int,
        weekdays: Iterable[int] | None,
        tz: str | None = None,
        enabled: bool = True,
    ) -> None:
        await asyncio.to_thread(
            self._add_sync, name, agent, task, hour, minute, weekdays, tz, enabled
        )

    def _add_sync(
        self,
        name: str,
        agent: str,
        task: str,
        hour: int,
        minute: int,
        weekdays: Iterable[int] | None,
        tz: str | None,
        enabled: bool,
    ) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_cron_entries
                    (name, agent, task, hour, minute, weekdays, tz, enabled, created_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    agent,
                    task,
                    hour,
                    minute,
                    encode_weekdays(weekdays),
                    tz or local_timezone_name(),
                    int(enabled),
                    now_epoch(),
                ),
            )

    async def unique_name(self, task: str) -> str:
        """A schedule key derived from *task* that is not already taken.

        Names come from the task text, truncated, so two different reminders can
        easily slug to the same key - "stretch in the morning" and "stretch in
        the evening" both did. The insert is INSERT OR REPLACE, so the collision
        was silent and the first schedule simply vanished.
        """
        base = schedule_name(task)
        taken = {row["name"] for row in await self.list()}
        if base not in taken:
            return base
        return next(f"{base}_{suffix}" for suffix in count(2) if f"{base}_{suffix}" not in taken)

    async def update(self, name: str, **fields: object) -> bool:
        """Change some fields of one entry. Returns False if no such entry exists.

        Fields left at :data:`UNSET` are untouched, so a caller can pass through
        a request that only names what the user actually asked to change - while
        an explicit None still clears the field it names.
        """
        changes = {k: v for k, v in fields.items() if k in _UPDATABLE and v is not UNSET}
        if "weekdays" in changes:
            changes["weekdays"] = encode_weekdays(changes["weekdays"])  # type: ignore[arg-type]
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
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
        "weekdays": decode_weekdays(row["weekdays"]),
        "tz": row["tz"],
        "enabled": bool(row["enabled"]),
        "created_epoch": row["created_epoch"],
    }
