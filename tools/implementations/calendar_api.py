"""Simulated Google Calendar API tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import AuthenticatedTool, ToolInput, ToolOutput
from utils import generate_id, open_db_connection


class CalendarApiTool(AuthenticatedTool):
    """Simulates Google Calendar operations with local SQLite persistence."""

    name = "calendar_api"
    description = "Manage calendar events (list, create, delete)."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the calendar tool and database."""
        self._db_path = db_path or (settings.north_home / "calendar.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    description TEXT
                )
                """
            )

    async def validate_credentials(self) -> bool:
        """Simulate credential verification."""
        return True

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute calendar actions."""
        action = input.params.get("action", "list_events")

        if action == "list_events":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM events").fetchall()
                events = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"events": events})

        elif action == "create_event":
            event_id = input.params.get("id") or f"evt_{generate_id()[:8]}"
            title = input.params.get("title")
            start_time = input.params.get("start_time")
            end_time = input.params.get("end_time")
            desc = input.params.get("description", "")

            if not (title and start_time and end_time):
                return ToolOutput(
                    success=False,
                    error="Missing required parameters: 'title', 'start_time', 'end_time'.",
                )

            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO events (id, title, start_time, end_time, description) VALUES (?, ?, ?, ?, ?)",
                    (event_id, title, start_time, end_time, desc),
                )
            return ToolOutput(success=True, data={"event_id": event_id, "status": "created"})

        elif action == "delete_event":
            event_id = input.params.get("id")
            if not event_id:
                return ToolOutput(success=False, error="Missing required parameter 'id'.")

            with open_db_connection(self._db_path) as conn:
                conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return ToolOutput(success=True, data={"event_id": event_id, "status": "deleted"})

        else:
            return ToolOutput(success=False, error=f"Unknown calendar action: {action}")
