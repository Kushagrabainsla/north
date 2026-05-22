"""Simulated LinkedIn career tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import AuthenticatedTool, ToolInput, ToolOutput
from utils import generate_id, open_db_connection


class LinkedinApiTool(AuthenticatedTool):
    """Simulates career prep and messaging drafting with local SQLite storage."""

    name = "linkedin_api"
    description = "Draft messages, retrieve professional profile, and check connections."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the career preparation database."""
        self._db_path = db_path or (settings.north_home / "linkedin.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema for connections and message drafts."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    connected BOOLEAN NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_drafts (
                    id TEXT PRIMARY KEY,
                    recipient_name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

            # Insert default connections if empty
            count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO contacts (id, name, title, company, connected) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("conn_1", "Jane Doe", "Engineering Manager", "LinkedIn", 1),
                        ("conn_2", "John Smith", "Recruiter", "Google", 0),
                        ("conn_3", "Alice Johnson", "Director of Product", "Netflix", 1),
                    ],
                )

    async def validate_credentials(self) -> bool:
        """Simulate credential check."""
        return True

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute LinkedIn actions."""
        action = input.params.get("action", "list_connections")

        if action == "list_connections":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM contacts WHERE connected = 1").fetchall()
                connections = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"connections": connections})

        elif action == "draft_message":
            recipient = input.params.get("recipient")
            subject = input.params.get("subject", "Networking outreach")
            body = input.params.get("body")

            if not (recipient and body):
                return ToolOutput(
                    success=False,
                    error="Missing required parameters: 'recipient', 'body'.",
                )

            draft_id = f"drft_{generate_id()[:8]}"
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO message_drafts (id, recipient_name, subject, body, status) VALUES (?, ?, ?, ?, ?)",
                    (draft_id, recipient, subject, body, "draft"),
                )
            return ToolOutput(success=True, data={"draft_id": draft_id, "status": "saved"})

        elif action == "get_drafts":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM message_drafts").fetchall()
                drafts = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"drafts": drafts})

        else:
            return ToolOutput(success=False, error=f"Unknown LinkedIn action: {action}")
