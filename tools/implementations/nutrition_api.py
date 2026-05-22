"""Simulated Nutrition database tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import Tool, ToolInput, ToolOutput
from utils import format_timestamp, generate_id, open_db_connection


class NutritionApiTool(Tool):
    """Simulates a nutrition search and logging database with local SQLite storage."""

    name = "nutrition_api"
    description = "Lookup food nutritional facts and log daily meal consumption."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the nutrition database."""
        self._db_path = db_path or (settings.north_home / "nutrition.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema with food data and meal logs."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS food_items (
                    name TEXT PRIMARY KEY,
                    calories REAL NOT NULL,
                    protein_g REAL NOT NULL,
                    carbs_g REAL NOT NULL,
                    fat_g REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meal_logs (
                    id TEXT PRIMARY KEY,
                    food_name TEXT NOT NULL,
                    servings REAL NOT NULL,
                    logged_at TEXT NOT NULL,
                    FOREIGN KEY (food_name) REFERENCES food_items (name)
                )
                """
            )

            # Seed default food items if empty
            count = conn.execute("SELECT COUNT(*) FROM food_items").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO food_items (name, calories, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("Oatmeal", 150.0, 5.0, 27.0, 2.5),
                        ("Chicken Breast", 165.0, 31.0, 0.0, 3.6),
                        ("Brown Rice", 215.0, 5.0, 45.0, 1.6),
                        ("Egg", 70.0, 6.0, 0.6, 5.0),
                        ("Banana", 105.0, 1.3, 27.0, 0.3),
                    ],
                )

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute nutrition actions."""
        action = input.params.get("action", "lookup_food")

        if action == "lookup_food":
            query = input.params.get("query")
            if not query:
                return ToolOutput(success=False, error="Missing parameter 'query'.")

            with open_db_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM food_items WHERE name LIKE ?", (f"%{query}%",)
                ).fetchone()
            if not row:
                return ToolOutput(
                    success=True,
                    data={"found": False, "message": f"No food found matching '{query}'"},
                )
            return ToolOutput(success=True, data={"found": True, "food": dict(row)})

        elif action == "log_meal":
            food_name = input.params.get("food_name")
            servings = input.params.get("servings", 1.0)
            if not food_name:
                return ToolOutput(success=False, error="Missing parameter 'food_name'.")

            # Check if food item exists
            with open_db_connection(self._db_path) as conn:
                row = conn.execute("SELECT * FROM food_items WHERE name = ?", (food_name,)).fetchone()
                if not row:
                    return ToolOutput(
                        success=False,
                        error=f"Food item '{food_name}' is not in database. Add it or use an existing one.",
                    )

                log_id = f"meal_{generate_id()[:8]}"
                logged_at = format_timestamp()
                conn.execute(
                    "INSERT INTO meal_logs (id, food_name, servings, logged_at) VALUES (?, ?, ?, ?)",
                    (log_id, food_name, servings, logged_at),
                )
            return ToolOutput(success=True, data={"log_id": log_id, "status": "logged"})

        elif action == "get_logs":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT m.id, m.food_name, m.servings, m.logged_at,
                           f.calories * m.servings as total_calories,
                           f.protein_g * m.servings as total_protein
                    FROM meal_logs m
                    JOIN food_items f ON m.food_name = f.name
                    ORDER BY m.logged_at DESC
                    """
                ).fetchall()
                logs = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"logs": logs})

        else:
            return ToolOutput(success=False, error=f"Unknown nutrition action: {action}")
