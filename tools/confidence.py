"""SQLite-backed confidence scores per (agent, tool). See README Section 7.5."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection

DEFAULT_CONFIDENCE: float = 0.5
# EMA smoothing factor: each new observation shifts the score 10 % toward the
# outcome (1.0 = helpful, 0.0 = unhelpful).  Recent experience dominates over
# time without the slow recovery of the old fixed-delta approach.
EMA_ALPHA: float = 0.10
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
    pair and is updated via exponential moving average (EMA_ALPHA = 0.10).
    A helpful use blends the score toward 1.0; unhelpful blends toward 0.0.
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
        now = datetime.now(UTC).isoformat()
        outcome = 1.0 if was_helpful else 0.0
        helpful_inc = 1 if was_helpful else 0

        with open_db_connection(self._db_path) as conn:
            existing = conn.execute(
                "SELECT confidence, uses_total, uses_helpful "
                "FROM tool_confidence WHERE agent = ? AND tool = ?",
                (agent, tool),
            ).fetchone()

            if existing is None:
                # Blend the default prior with the first observation.
                new_score = _clamp(EMA_ALPHA * outcome + (1.0 - EMA_ALPHA) * DEFAULT_CONFIDENCE)
                conn.execute(
                    "INSERT INTO tool_confidence "
                    "(agent, tool, confidence, uses_total, uses_helpful, last_updated) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (agent, tool, new_score, helpful_inc, now),
                )
                return new_score

            new_score = _clamp(EMA_ALPHA * outcome + (1.0 - EMA_ALPHA) * existing["confidence"])
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

    async def seed_defaults(self, graph: dict[str, list[str]], reliable_tools: frozenset[str]) -> None:
        """Insert default confidence scores for all (agent, tool) pairs, if not yet recorded.

        Rows already written by real usage are preserved via INSERT OR IGNORE, so seeds
        never overwrite earned data. Call once at startup before any tasks run.

        Args:
            graph:          {agent_name: [tool_names]} — the full tool graph.
            reliable_tools: Tool names that get a high prior (0.80). Everything
                            else receives the neutral default (0.50).
        """
        await asyncio.to_thread(self._seed_defaults_sync, graph, reliable_tools)

    def _seed_defaults_sync(self, graph: dict[str, list[str]], reliable_tools: frozenset[str]) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            for agent, tools in graph.items():
                for tool in tools:
                    score = 0.80 if tool in reliable_tools else DEFAULT_CONFIDENCE
                    conn.execute(
                        "INSERT OR IGNORE INTO tool_confidence "
                        "(agent, tool, confidence, uses_total, uses_helpful, last_updated) "
                        "VALUES (?, ?, ?, 0, 0, ?)",
                        (agent, tool, score, now),
                    )

    async def inherit_from(self, new_agent: str, source_agent: str) -> None:
        """Copy `source_agent`'s tool rows to `new_agent` as a starting prior.

        Idempotent: existing rows on `new_agent` are preserved (`INSERT OR IGNORE`).
        See README 7.5 ("similar_to" inheritance).
        """
        await asyncio.to_thread(self._inherit_from_sync, new_agent, source_agent)

    def _inherit_from_sync(self, new_agent: str, source_agent: str) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_confidence "
                "(agent, tool, confidence, uses_total, uses_helpful, last_updated) "
                "SELECT ?, tool, confidence, 0, 0, ? "
                "FROM tool_confidence WHERE agent = ?",
                (new_agent, now, source_agent),
            )
