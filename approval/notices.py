"""What north delivered to the user, kept until they dismiss it.

A schedule runs while nobody is watching, so what it has to say must wait
somewhere the user will look. It was sent only to the system notifiers (the TUI,
Telegram, macOS), and a dashboard-only install has none of them: in the issue #76
test a reminder fired on time, its step said `"reminder_sent": true`, and nothing
appeared anywhere. The dashboard is the channel every install has, so every
delivery is stored here, shown on it, and is the record that it was delivered.

Not cards: a notice asks nothing. Kept apart from the approval store, a notice
never looks like work waiting on a decision.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection
from utils.ids import generate_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    message       TEXT NOT NULL,
    source        TEXT NOT NULL,
    task_id       TEXT,
    created_at    TEXT NOT NULL,
    dismissed_at  TEXT
)
"""
# Undismissed notices shown at once; older ones stay stored.
_SHOWN = 50


@dataclass(frozen=True)
class Notice:
    """One message north delivered: a reminder, or how a scheduled run ended."""

    id: str
    title: str
    message: str
    source: str
    task_id: str | None
    created_at: str
    dismissed_at: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


class NoticeStore:
    """Notices on disk, or in memory without a *db_path* (tests, an embedded run)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._memory: dict[str, Notice] = {}
        self._lock = threading.Lock()
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with open_db_connection(db_path) as conn:
                conn.execute(_SCHEMA)

    def add(self, *, title: str, message: str, source: str, task_id: str | None = None) -> Notice:
        notice = Notice(
            id=generate_id(),
            title=title,
            message=message,
            source=source,
            task_id=task_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        if self._db_path is None:
            with self._lock:
                self._memory[notice.id] = notice
            return notice
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO notices (id, title, message, source, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (notice.id, title, message, source, task_id, notice.created_at),
            )
        return notice

    def waiting(self) -> list[Notice]:
        """Undismissed notices, newest first."""
        if self._db_path is None:
            with self._lock:
                notices = [n for n in self._memory.values() if n.dismissed_at is None]
            return sorted(notices, key=lambda n: n.created_at, reverse=True)[:_SHOWN]
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM notices WHERE dismissed_at IS NULL ORDER BY created_at DESC LIMIT ?", (_SHOWN,)
            ).fetchall()
        return [Notice(**dict(row)) for row in rows]

    def dismiss(self, notice_id: str) -> bool:
        """Mark a notice read. False when there is no such undismissed notice."""
        now = datetime.now(UTC).isoformat()
        if self._db_path is None:
            with self._lock:
                notice = self._memory.get(notice_id)
                if notice is None or notice.dismissed_at is not None:
                    return False
                self._memory[notice_id] = replace(notice, dismissed_at=now)
                return True
        with open_db_connection(self._db_path) as conn:
            return (
                conn.execute(
                    "UPDATE notices SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL", (now, notice_id)
                ).rowcount
                > 0
            )
