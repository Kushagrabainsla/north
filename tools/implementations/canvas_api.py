"""Simulated Canvas API tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import AuthenticatedTool, ToolInput, ToolOutput
from utils import generate_id, open_db_connection


class CanvasApiTool(AuthenticatedTool):
    """Simulates Canvas academic platform operations with local SQLite persistence."""

    name = "canvas_api"
    description = "Read academic courses, assignments, and deadlines."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the Canvas tool and database."""
        self._db_path = db_path or (settings.north_home / "canvas.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema with default mock courses and assignments."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (course_id) REFERENCES courses (id)
                )
                """
            )

            # Insert mock data if empty
            count = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO courses (id, code, name) VALUES (?, ?, ?)",
                    [
                        ("cs61a", "CS 61A", "Structure and Interpretation of Computer Programs"),
                        ("cs162", "CS 162", "Operating Systems and System Programming"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO assignments (id, course_id, title, due_date, status) VALUES (?, ?, ?, ?, ?)",
                    [
                        (f"asn_{generate_id()[:8]}", "cs61a", "Project 4: Scheme Interpreter", "2026-05-30T23:59:59Z", "pending"),
                        (f"asn_{generate_id()[:8]}", "cs162", "Project 2: User Programs", "2026-06-05T23:59:59Z", "pending"),
                    ],
                )

    async def validate_credentials(self) -> bool:
        """Simulate Canvas credential check."""
        return True

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute Canvas retrieval actions."""
        action = input.params.get("action", "list_courses")

        if action == "list_courses":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM courses").fetchall()
                courses = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"courses": courses})

        elif action == "list_assignments":
            course_id = input.params.get("course_id")
            with open_db_connection(self._db_path) as conn:
                if course_id:
                    rows = conn.execute(
                        "SELECT * FROM assignments WHERE course_id = ?", (course_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM assignments").fetchall()
                assignments = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"assignments": assignments})

        elif action == "update_assignment_status":
            assignment_id = input.params.get("id")
            status = input.params.get("status")
            if not (assignment_id and status):
                return ToolOutput(
                    success=False,
                    error="Missing required parameters: 'id', 'status'.",
                )

            with open_db_connection(self._db_path) as conn:
                # Check if assignment exists
                row = conn.execute("SELECT 1 FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
                if not row:
                    return ToolOutput(success=False, error=f"Assignment '{assignment_id}' not found.")
                conn.execute(
                    "UPDATE assignments SET status = ? WHERE id = ?",
                    (status, assignment_id),
                )
            return ToolOutput(success=True, data={"id": assignment_id, "status": status})

        else:
            return ToolOutput(success=False, error=f"Unknown canvas action: {action}")
