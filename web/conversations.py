"""Durable conversations shared by every North interface."""

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
    workspace   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'web',
    goal        TEXT NOT NULL DEFAULT '',
    goal_status TEXT NOT NULL DEFAULT 'idle',
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
    workspace: str
    source: str
    goal: str
    goal_status: str
    pinned: bool
    archived: bool
    created_at: str
    updated_at: str
    turn_count: int


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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(web_conversations)")}
            if "workspace" not in columns:
                conn.execute("ALTER TABLE web_conversations ADD COLUMN workspace TEXT NOT NULL DEFAULT ''")
            if "source" not in columns:
                conn.execute("ALTER TABLE web_conversations ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
            if "goal" not in columns:
                conn.execute("ALTER TABLE web_conversations ADD COLUMN goal TEXT NOT NULL DEFAULT ''")
            if "goal_status" not in columns:
                conn.execute("ALTER TABLE web_conversations ADD COLUMN goal_status TEXT NOT NULL DEFAULT 'idle'")
            # Existing sessions predate explicit goals. Their first prompt is
            # the best durable statement of intent and is already local data.
            conn.execute(
                """UPDATE web_conversations
                   SET goal = COALESCE((
                       SELECT prompt FROM web_turns
                       WHERE web_turns.conversation_id = web_conversations.id
                       ORDER BY position ASC LIMIT 1
                   ), '')
                   WHERE goal = ''"""
            )
            conn.execute(
                "UPDATE web_conversations SET goal_status='active' WHERE goal != '' AND goal_status='idle'"
            )
            conn.execute(
                """UPDATE web_conversations
                   SET title = SUBSTR(COALESCE((
                       SELECT TRIM(prompt) FROM web_turns
                       WHERE web_turns.conversation_id = web_conversations.id
                       ORDER BY position ASC LIMIT 1
                   ), title), 1, 72)
                   WHERE title IN ('New chat', 'New session')
                     AND EXISTS (
                       SELECT 1 FROM web_turns WHERE web_turns.conversation_id = web_conversations.id
                   )"""
            )

    async def create(self, title: str = "New chat", workspace: str = "", source: str = "web") -> Conversation:
        return await asyncio.to_thread(self._create_sync, title, workspace, source)

    def _create_sync(self, title: str, workspace: str, source: str) -> Conversation:
        conversation_id = generate_id()
        now = format_timestamp(utcnow())
        clean_title = title.strip()[:160] or "New chat"
        clean_source = source if source in {"web", "cli"} else "web"
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """INSERT INTO web_conversations(id, title, workspace, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (conversation_id, clean_title, workspace, clean_source, now, now),
            )
        return Conversation(
            id=conversation_id,
            title=clean_title,
            workspace=workspace,
            source=clean_source,
            goal="",
            goal_status="idle",
            pinned=False,
            archived=False,
            created_at=now,
            updated_at=now,
            turn_count=0,
        )

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
        sql = """SELECT web_conversations.*,
                        (SELECT COUNT(*) FROM web_turns
                         WHERE web_turns.conversation_id = web_conversations.id) AS turn_count
                 FROM web_conversations WHERE archived=?"""
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
        workspace: str | None = None,
        goal: str | None = None,
        goal_status: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        touch_updated_at: bool = True,
    ) -> Conversation | None:
        await asyncio.to_thread(
            self._update_sync,
            conversation_id,
            title,
            workspace,
            goal,
            goal_status,
            pinned,
            archived,
            touch_updated_at,
        )
        return await self.get(conversation_id)

    async def delete(self, conversation_id: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, conversation_id)

    def _delete_sync(self, conversation_id: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            result = conn.execute("DELETE FROM web_conversations WHERE id=?", (conversation_id,))
            return result.rowcount > 0

    def _update_sync(
        self,
        conversation_id: str,
        title: str | None,
        workspace: str | None,
        goal: str | None,
        goal_status: str | None,
        pinned: bool | None,
        archived: bool | None,
        touch_updated_at: bool,
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if touch_updated_at:
            assignments.append("updated_at=?")
            params.append(format_timestamp(utcnow()))
        if title is not None:
            assignments.append("title=?")
            params.append(title.strip()[:160] or "New chat")
        if workspace is not None:
            assignments.append("workspace=?")
            params.append(workspace)
        if goal is not None:
            assignments.append("goal=?")
            params.append(goal.strip()[:4000])
        if goal_status is not None:
            if goal_status not in {"idle", "active", "waiting_for_user", "blocked", "achieved"}:
                raise ValueError(f"Invalid goal status: {goal_status}")
            assignments.append("goal_status=?")
            params.append(goal_status)
        if pinned is not None:
            assignments.append("pinned=?")
            params.append(int(pinned))
        if archived is not None:
            assignments.append("archived=?")
            params.append(int(archived))
        if not assignments:
            return
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
            row = conn.execute(
                "SELECT title, goal, goal_status FROM web_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
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
            if title in {"New chat", "New session"}:
                title = " ".join(prompt.strip().split())[:72] or title
            goal = row["goal"]
            goal_status = row["goal_status"]
            if not goal or goal_status == "achieved":
                goal = " ".join(prompt.strip().split())[:4000]
            # A new user turn resumes work after a question or blocker.
            goal_status = "active"
            conn.execute(
                "UPDATE web_conversations SET title=?, goal=?, goal_status=?, updated_at=? WHERE id=?",
                (title, goal, goal_status, now, conversation_id),
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
        try:
            turn_count = int(row["turn_count"])
        except IndexError:
            turn_count = 0
        return Conversation(
            id=row["id"],
            title=row["title"],
            workspace=row["workspace"],
            source=row["source"],
            goal=row["goal"],
            goal_status=row["goal_status"],
            pinned=bool(row["pinned"]),
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            turn_count=turn_count,
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
