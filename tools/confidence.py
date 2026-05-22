"""SQLite-backed confidence scores per (agent, tool). See README Section 7.5."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from utils.db import open_db_connection

DEFAULT_CONFIDENCE: float = 0.5
CONFIDENCE_INCREASE: float = 0.05
CONFIDENCE_DECREASE: float = 0.03
MIN_CONFIDENCE: float = 0.0
MAX_CONFIDENCE: float = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_confidence (
    agent           TEXT NOT NULL,
    tool            TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.5,
    uses_total      INTEGER NOT NULL DEFAULT 0,
    uses_helpful    INTEGER NOT NULL DEFAULT 0,
    last_updated    DATETIME NOT NULL,
    PRIMARY KEY (agent, tool)
)
"""


def _clamp(value: float) -> float:
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, value))


class ConfidenceTracker:
    """Tracks how reliable each tool has been for each agent.

    Confidence starts at `DEFAULT_CONFIDENCE` for any unseen (agent, tool)
    pair. A helpful use adds `CONFIDENCE_INCREASE`; an unhelpful one
    subtracts `CONFIDENCE_DECREASE`. Scores are clamped to [0.0, 1.0].
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    async def get_score(self, agent: str, tool: str) -> float:
        row = await asyncio.to_thread(self._get_row_sync, agent, tool)
        return row["confidence"] if row is not None else DEFAULT_CONFIDENCE

    def _get_row_sync(self, agent: str, tool: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute(
                "SELECT * FROM tool_confidence WHERE agent = ? AND tool = ?",
                (agent, tool),
            ).fetchone()

    async def record_use(self, agent: str, tool: str, was_helpful: bool) -> float:
        """Update the (agent, tool) score and return the new value."""
        return await asyncio.to_thread(
            self._record_use_sync, agent, tool, was_helpful
        )

    def _record_use_sync(self, agent: str, tool: str, was_helpful: bool) -> float:
        now = datetime.now(timezone.utc).isoformat()
        delta = CONFIDENCE_INCREASE if was_helpful else -CONFIDENCE_DECREASE
        helpful_inc = 1 if was_helpful else 0

        with open_db_connection(self._db_path) as conn:
            existing = conn.execute(
                "SELECT confidence, uses_total, uses_helpful "
                "FROM tool_confidence WHERE agent = ? AND tool = ?",
                (agent, tool),
            ).fetchone()

            if existing is None:
                new_score = _clamp(DEFAULT_CONFIDENCE + delta)
                conn.execute(
                    "INSERT INTO tool_confidence "
                    "(agent, tool, confidence, uses_total, uses_helpful, last_updated) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (agent, tool, new_score, helpful_inc, now),
                )
                return new_score

            new_score = _clamp(existing["confidence"] + delta)
            conn.execute(
                "UPDATE tool_confidence "
                "SET confidence = ?, "
                "    uses_total = uses_total + 1, "
                "    uses_helpful = uses_helpful + ?, "
                "    last_updated = ? "
                "WHERE agent = ? AND tool = ?",
                (new_score, helpful_inc, now, agent, tool),
            )
            return new_score

    async def scores_for_agent(self, agent: str) -> list[tuple[str, float]]:
        """Return (tool_name, score) pairs for `agent`, ordered by score descending."""
        rows = await asyncio.to_thread(self._scores_for_agent_sync, agent)
        return [(r["tool"], r["confidence"]) for r in rows]

    def _scores_for_agent_sync(self, agent: str) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            return list(
                conn.execute(
                    "SELECT tool, confidence FROM tool_confidence "
                    "WHERE agent = ? ORDER BY confidence DESC, tool ASC",
                    (agent,),
                ).fetchall()
            )

    async def inherit_from(self, new_agent: str, source_agent: str) -> None:
        """Copy `source_agent`'s tool rows to `new_agent` as a starting prior.

        Idempotent: existing rows on `new_agent` are preserved (`INSERT OR IGNORE`).
        See README 7.5 ("similar_to" inheritance).
        """
        await asyncio.to_thread(self._inherit_from_sync, new_agent, source_agent)

    def _inherit_from_sync(self, new_agent: str, source_agent: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_confidence "
                "(agent, tool, confidence, uses_total, uses_helpful, last_updated) "
                "SELECT ?, tool, confidence, 0, 0, ? "
                "FROM tool_confidence WHERE agent = ?",
                (new_agent, now, source_agent),
            )
