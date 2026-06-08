"""Persistent storage for user-defined cron entries in the jobs SQLite DB."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from utils.db import open_db_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_cron_entries (
    name       TEXT PRIMARY KEY,
    agent      TEXT NOT NULL,
    task       TEXT NOT NULL,
    hour       INTEGER NOT NULL,
    minute     INTEGER NOT NULL,
    weekday    INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


class UserCronStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    async def add(self, name: str, agent: str, task: str, hour: int, minute: int, weekday: int | None) -> None:
        await asyncio.to_thread(self._add_sync, name, agent, task, hour, minute, weekday)

    def _add_sync(self, name: str, agent: str, task: str, hour: int, minute: int, weekday: int | None) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_cron_entries (name, agent, task, hour, minute, weekday)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, agent, task, hour, minute, weekday),
            )

    async def remove(self, name: str) -> None:
        await asyncio.to_thread(self._remove_sync, name)

    def _remove_sync(self, name: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM user_cron_entries WHERE name = ?", (name,))

    async def list(self) -> list[dict]:
        rows = await asyncio.to_thread(self._list_sync)
        return [
            {
                "name": r["name"],
                "agent": r["agent"],
                "task": r["task"],
                "hour": r["hour"],
                "minute": r["minute"],
                "weekday": r["weekday"],
            }
            for r in rows
        ]

    def _list_sync(self) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(conn.execute("SELECT * FROM user_cron_entries ORDER BY created_at").fetchall())
