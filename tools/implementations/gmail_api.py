"""Simulated Gmail API tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import AuthenticatedTool, ToolInput, ToolOutput
from utils import format_timestamp, generate_id, open_db_connection


class GmailApiTool(AuthenticatedTool):
    """Simulates Gmail operations with local SQLite persistence."""

    name = "gmail_api"
    description = "Read, search, and send emails."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the Gmail tool and database."""
        self._db_path = db_path or (settings.north_home / "gmail.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            # Insert initial mock emails if empty
            count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
            if count == 0:
                conn.execute(
                    """
                    INSERT INTO emails (id, sender, recipient, subject, body, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"msg_{generate_id()[:8]}",
                        "professor@university.edu",
                        "me@north.net",
                        "Thesis Guideline Changes",
                        "Dear student, please note the thesis guidelines have been updated. Ensure you use raw SQLite.",
                        format_timestamp(),
                    ),
                )

    async def validate_credentials(self) -> bool:
        """Simulate credential verification."""
        return True

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute Gmail actions."""
        action = input.params.get("action", "list_emails")

        if action == "list_emails":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM emails ORDER BY timestamp DESC").fetchall()
                emails = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"emails": emails})

        elif action == "send_email":
            recipient = input.params.get("recipient")
            subject = input.params.get("subject")
            body = input.params.get("body")

            if not (recipient and subject and body):
                return ToolOutput(
                    success=False,
                    error="Missing required parameters: 'recipient', 'subject', 'body'.",
                )

            email_id = f"msg_{generate_id()[:8]}"
            timestamp = format_timestamp()

            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO emails (id, sender, recipient, subject, body, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    (email_id, "me@north.net", recipient, subject, body, timestamp),
                )
            return ToolOutput(success=True, data={"email_id": email_id, "status": "sent"})

        elif action == "get_email":
            email_id = input.params.get("id")
            if not email_id:
                return ToolOutput(success=False, error="Missing required parameter 'id'.")

            with open_db_connection(self._db_path) as conn:
                row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
            if not row:
                return ToolOutput(success=False, error=f"Email with id {email_id} not found.")

            return ToolOutput(success=True, data={"email": dict(row)})

        else:
            return ToolOutput(success=False, error=f"Unknown email action: {action}")
