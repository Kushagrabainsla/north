"""SQLite-backed confidence scores per (agent, tool). See README Section 7.5."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE: float = 0.5
EMA_ALPHA: float = 0.10
MIN_CONFIDENCE: float = 0.0
MAX_CONFIDENCE: float = 1.0
# Confidence below this threshold triggers a degradation warning.
_WARN_THRESHOLD: float = 0.25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_confidence (
    agent                TEXT NOT NULL,
    tool                 TEXT NOT NULL,
    confidence           REAL NOT NULL DEFAULT 0.5,
    uses_total           INTEGER NOT NULL DEFAULT 0,
    uses_helpful         INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_updated         DATETIME NOT NULL,
    PRIMARY KEY (agent, tool)
)
"""

_MODEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_confidence (
    model_id     TEXT NOT NULL,
    provider     TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0.5,
    uses_total   INTEGER NOT NULL DEFAULT 0,
    last_updated DATETIME NOT NULL,
    PRIMARY KEY (model_id, provider)
)
"""

_MIGRATION_ADD_CONSECUTIVE_FAILURES = (
    "ALTER TABLE tool_confidence ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
)


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
            conn.execute(_MODEL_SCHEMA)
            import contextlib
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(_MIGRATION_ADD_CONSECUTIVE_FAILURES)

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
                "SELECT confidence, uses_total, uses_helpful, consecutive_failures "
                "FROM tool_confidence WHERE agent = ? AND tool = ?",
                (agent, tool),
            ).fetchone()

            if existing is None:
                new_score = _clamp(EMA_ALPHA * outcome + (1.0 - EMA_ALPHA) * DEFAULT_CONFIDENCE)
                new_consecutive = 0 if was_helpful else 1
                conn.execute(
                    "INSERT INTO tool_confidence "
                    "(agent, tool, confidence, uses_total, uses_helpful, consecutive_failures, last_updated) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (agent, tool, new_score, helpful_inc, new_consecutive, now),
                )
                return new_score

            prev_consecutive = existing["consecutive_failures"]
            if was_helpful:
                alpha = EMA_ALPHA
                new_consecutive = 0
            else:
                # Scale alpha on consecutive failures: 0.1 → 0.2 → 0.4 (cap at 3rd+)
                alpha = min(0.5, EMA_ALPHA * (2 ** min(prev_consecutive, 2)))
                new_consecutive = prev_consecutive + 1

            new_score = _clamp(alpha * outcome + (1.0 - alpha) * existing["confidence"])
            conn.execute(
                "UPDATE tool_confidence "
                "SET confidence = ?, "
                "    uses_total = uses_total + 1, "
                "    uses_helpful = uses_helpful + ?, "
                "    consecutive_failures = ?, "
                "    last_updated = ? "
                "WHERE agent = ? AND tool = ?",
                (new_score, helpful_inc, new_consecutive, now, agent, tool),
            )
            if new_score < _WARN_THRESHOLD:
                logger.warning(
                    "tool degradation: %s/%s confidence=%.2f consecutive_failures=%d",
                    agent, tool, new_score, new_consecutive,
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
                        "(agent, tool, confidence, uses_total, uses_helpful, consecutive_failures, last_updated) "
                        "VALUES (?, ?, ?, 0, 0, 0, ?)",
                        (agent, tool, score, now),
                    )

    def load_model_scores_sync(self) -> dict[tuple[str, str], tuple[float, int]]:
        """Return all persisted model scores as {(model_id, provider): (score, uses)}.

        Synchronous so ModelDispatcher can call it from __init__ at startup.
        """
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT model_id, provider, confidence, uses_total FROM model_confidence"
            ).fetchall()
        return {(r["model_id"], r["provider"]): (r["confidence"], r["uses_total"]) for r in rows}

    async def save_model_score(
        self, model_id: str, provider: str, score: float, uses: int
    ) -> None:
        """Upsert one model's EMA score. Called fire-and-forget after each dispatch outcome."""
        await asyncio.to_thread(self._save_model_score_sync, model_id, provider, score, uses)

    def _save_model_score_sync(
        self, model_id: str, provider: str, score: float, uses: int
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO model_confidence "
                "(model_id, provider, confidence, uses_total, last_updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (model_id, provider, score, uses, now),
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
                "(agent, tool, confidence, uses_total, uses_helpful, consecutive_failures, last_updated) "
                "SELECT ?, tool, confidence, 0, 0, 0, ? "
                "FROM tool_confidence WHERE agent = ?",
                (new_agent, now, source_agent),
            )
