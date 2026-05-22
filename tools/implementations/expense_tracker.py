"""Simulated expense tracker tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import Tool, ToolInput, ToolOutput
from utils import format_timestamp, generate_id, open_db_connection


class ExpenseTrackerTool(Tool):
    """Simulates personal finance and expense logging using local SQLite."""

    name = "expense_tracker"
    description = "Log personal expenditures, categorise them, and fetch transaction history."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the expense tracker database."""
        self._db_path = db_path or (settings.north_home / "expenses.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema for transactions."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    amount REAL NOT NULL,
                    description TEXT,
                    logged_at TEXT NOT NULL
                )
                """
            )

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute expense tracker actions."""
        action = input.params.get("action", "list_expenses")

        if action == "list_expenses":
            category = input.params.get("category")
            with open_db_connection(self._db_path) as conn:
                if category:
                    rows = conn.execute(
                        "SELECT * FROM transactions WHERE category = ? ORDER BY logged_at DESC",
                        (category,),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM transactions ORDER BY logged_at DESC").fetchall()
                transactions = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"transactions": transactions})

        elif action == "log_expense":
            category = input.params.get("category")
            amount = input.params.get("amount")
            description = input.params.get("description", "")

            if not (category and amount is not None):
                return ToolOutput(
                    success=False,
                    error="Missing required parameters: 'category', 'amount'.",
                )

            try:
                amount_float = float(amount)
            except ValueError:
                return ToolOutput(success=False, error="Parameter 'amount' must be a numeric value.")

            log_id = f"tx_{generate_id()[:8]}"
            logged_at = format_timestamp()

            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO transactions (id, category, amount, description, logged_at) VALUES (?, ?, ?, ?, ?)",
                    (log_id, category, amount_float, description, logged_at),
                )
            return ToolOutput(success=True, data={"transaction_id": log_id, "status": "logged"})

        elif action == "get_summary":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT category, SUM(amount) as total_amount, COUNT(*) as count
                    FROM transactions
                    GROUP BY category
                    """
                ).fetchall()
                summary = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"summary": summary})

        else:
            return ToolOutput(success=False, error=f"Unknown expense action: {action}")
