"""Simulated fitness tracking tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import Tool, ToolInput, ToolOutput
from utils import format_timestamp, generate_id, open_db_connection


class FitnessTrackerTool(Tool):
    """Simulates workout logging and physical activity tracking using local SQLite."""

    name = "fitness_tracker"
    description = "Check exercise plans, log exercises, and retrieve daily activity logs."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the fitness tracking database."""
        self._db_path = db_path or (settings.north_home / "fitness.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema for workout logging."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exercises (
                    name TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    target_muscles TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workout_logs (
                    id TEXT PRIMARY KEY,
                    exercise_name TEXT NOT NULL,
                    sets INTEGER NOT NULL,
                    reps INTEGER NOT NULL,
                    weight_lbs REAL NOT NULL,
                    logged_at TEXT NOT NULL,
                    FOREIGN KEY (exercise_name) REFERENCES exercises (name)
                )
                """
            )

            # Seed initial exercises if empty
            count = conn.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO exercises (name, category, target_muscles) VALUES (?, ?, ?)",
                    [
                        ("Squat", "Strength", "Quads, Glutes"),
                        ("Bench Press", "Strength", "Chest, Triceps"),
                        ("Deadlift", "Strength", "Hamstrings, Lower Back"),
                        ("Pull-up", "Bodyweight", "Lats, Biceps"),
                        ("Running", "Cardio", "Cardiovascular"),
                    ],
                )

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute fitness tracking actions."""
        action = input.params.get("action", "list_exercises")

        if action == "list_exercises":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM exercises").fetchall()
                exercises = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"exercises": exercises})

        elif action == "log_workout":
            exercise_name = input.params.get("exercise_name")
            sets = input.params.get("sets", 1)
            reps = input.params.get("reps", 1)
            weight = input.params.get("weight_lbs", 0.0)

            if not exercise_name:
                return ToolOutput(success=False, error="Missing parameter 'exercise_name'.")

            with open_db_connection(self._db_path) as conn:
                row = conn.execute("SELECT 1 FROM exercises WHERE name = ?", (exercise_name,)).fetchone()
                if not row:
                    return ToolOutput(
                        success=False,
                        error=f"Exercise '{exercise_name}' is not in database. Add it or log a valid one.",
                    )

                log_id = f"fit_{generate_id()[:8]}"
                logged_at = format_timestamp()
                conn.execute(
                    "INSERT INTO workout_logs (id, exercise_name, sets, reps, weight_lbs, logged_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (log_id, exercise_name, sets, reps, weight, logged_at),
                )
            return ToolOutput(success=True, data={"log_id": log_id, "status": "logged"})

        elif action == "get_logs":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT * FROM workout_logs ORDER BY logged_at DESC"
                ).fetchall()
                logs = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"logs": logs})

        else:
            return ToolOutput(success=False, error=f"Unknown fitness action: {action}")
