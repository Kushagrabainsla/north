"""Simulated market data API tool using local SQLite persistence.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import settings
from tools import AuthenticatedTool, ToolInput, ToolOutput
from utils import open_db_connection


class MarketDataApiTool(AuthenticatedTool):
    """Simulates financial market data access with local SQLite persistence."""

    name = "market_data_api"
    description = "Retrieve current prices, volume, and information for financial assets."

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the market database."""
        self._db_path = db_path or (settings.north_home / "market.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema and default stocks/cryptos."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    change_pct REAL NOT NULL,
                    volume TEXT NOT NULL
                )
                """
            )

            # Insert default stocks and crypto if empty
            count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO assets (symbol, name, price, change_pct, volume) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("AAPL", "Apple Inc.", 175.50, 1.2, "52M"),
                        ("GOOG", "Alphabet Inc.", 148.20, -0.4, "28M"),
                        ("MSFT", "Microsoft Corp.", 415.10, 0.8, "22M"),
                        ("BTC", "Bitcoin", 67200.00, 4.5, "35B"),
                        ("ETH", "Ethereum", 3520.00, 3.1, "18B"),
                    ],
                )

    async def validate_credentials(self) -> bool:
        """Simulate authentication validation."""
        return True

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute market data actions."""
        action = input.params.get("action", "get_quote")

        if action == "get_quote":
            symbol = input.params.get("symbol")
            if not symbol:
                return ToolOutput(success=False, error="Missing parameter 'symbol'.")

            symbol_upper = symbol.upper()
            with open_db_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM assets WHERE symbol = ?", (symbol_upper,)
                ).fetchone()

            if not row:
                return ToolOutput(
                    success=False,
                    error=f"Asset symbol '{symbol_upper}' not found in the database.",
                )

            return ToolOutput(success=True, data={"quote": dict(row)})

        elif action == "list_assets":
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM assets").fetchall()
                assets = [dict(row) for row in rows]
            return ToolOutput(success=True, data={"assets": assets})

        else:
            return ToolOutput(success=False, error=f"Unknown market action: {action}")
