"""Durable chat conversations for the browser interface."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from utils.db import open_db_connection
from utils.ids import generate_id
from utils.time import format_timestamp, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES web_conversations(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    prompt          TEXT NOT NULL,
    task_id         TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(conversation_id, position)
);

CREATE INDEX IF NOT EXISTS idx_web_conversations_updated
    ON web_conversations(archived, pinned DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_turns_conversation
    ON web_turns(conversation_id, position);
"""


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    pinned: bool
    archived: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    position: int
    prompt: str
    task_id: str | None
    created_at: str


class ConversationStore:
    """SQLite-backed conversation and ordered-turn index."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(db_path) as conn:
            conn.executescript(_SCHEMA)

    async def create(self, title: str = "New chat") -> Conversation:
        return await asyncio.to_thread(self._create_sync, title)

    def _create_sync(self, title: str) -> Conversation:
        conversation_id = generate_id()
        now = format_timestamp(utcnow())
        clean_title = title.strip()[:160] or "New chat"
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO web_conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, clean_title, now, now),
            )
        return Conversation(conversation_id, clean_title, False, False, now, now)

    async def get(self, conversation_id: str) -> Conversation | None:
        row = await asyncio.to_thread(self._get_sync, conversation_id)
        return self._conversation(row) if row else None

    def _get_sync(self, conversation_id: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("SELECT * FROM web_conversations WHERE id=?", (conversation_id,)).fetchone()

    async def list(self, *, query: str = "", archived: bool = False, limit: int = 100) -> list[Conversation]:
        rows = await asyncio.to_thread(self._list_sync, query, archived, limit)
        return [self._conversation(row) for row in rows]

    def _list_sync(self, query: str, archived: bool, limit: int) -> list[sqlite3.Row]:
        sql = "SELECT * FROM web_conversations WHERE archived=?"
        params: list[object] = [int(archived)]
        if query.strip():
            sql += " AND title LIKE ? ESCAPE '\\'"
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with open_db_connection(self._db_path) as conn:
            return list(conn.execute(sql, params))

    async def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> Conversation | None:
        await asyncio.to_thread(self._update_sync, conversation_id, title, pinned, archived)
        return await self.get(conversation_id)

    def _update_sync(
        self,
        conversation_id: str,
        title: str | None,
        pinned: bool | None,
        archived: bool | None,
    ) -> None:
        assignments = ["updated_at=?"]
        params: list[object] = [format_timestamp(utcnow())]
        if title is not None:
            assignments.append("title=?")
            params.append(title.strip()[:160] or "New chat")
        if pinned is not None:
            assignments.append("pinned=?")
            params.append(int(pinned))
        if archived is not None:
            assignments.append("archived=?")
            params.append(int(archived))
        params.append(conversation_id)
        with open_db_connection(self._db_path) as conn:
            conn.execute(f"UPDATE web_conversations SET {', '.join(assignments)} WHERE id=?", params)

    async def add_turn(self, conversation_id: str, prompt: str) -> Turn:
        return await asyncio.to_thread(self._add_turn_sync, conversation_id, prompt)

    def _add_turn_sync(self, conversation_id: str, prompt: str) -> Turn:
        turn_id = generate_id()
        now = format_timestamp(utcnow())
        with open_db_connection(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT title FROM web_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise LookupError("Conversation not found")
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM web_turns WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """INSERT INTO web_turns(id, conversation_id, position, prompt, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (turn_id, conversation_id, position, prompt, now),
            )
            title = row["title"]
            if title == "New chat":
                title = " ".join(prompt.strip().split())[:72] or title
            conn.execute(
                "UPDATE web_conversations SET title=?, updated_at=? WHERE id=?",
                (title, now, conversation_id),
            )
        return Turn(turn_id, conversation_id, position, prompt, None, now)

    async def attach_task(self, turn_id: str, task_id: str) -> None:
        await asyncio.to_thread(self._attach_task_sync, turn_id, task_id)

    def _attach_task_sync(self, turn_id: str, task_id: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("UPDATE web_turns SET task_id=? WHERE id=?", (task_id, turn_id))

    async def turns(self, conversation_id: str) -> list[Turn]:
        rows = await asyncio.to_thread(self._turns_sync, conversation_id)
        return [self._turn(row) for row in rows]

    def _turns_sync(self, conversation_id: str) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM web_turns WHERE conversation_id=? ORDER BY position",
                    (conversation_id,),
                )
            )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            title=row["title"],
            pinned=bool(row["pinned"]),
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        return Turn(
            id=row["id"],
            conversation_id=row["conversation_id"],
            position=int(row["position"]),
            prompt=row["prompt"],
            task_id=row["task_id"],
            created_at=row["created_at"],
        )
