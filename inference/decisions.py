"""The routing decision log: why each part ran on the model it ran on.

Built first, and not optional. A derived order is harder to reason about than a
written one, and this is the whole mitigation: one row per selection carrying the
part, the requirements that were derived for it, how many models were considered,
every skip with its reason, the winner, and the outcome.

    "why did the coder run on a free model?"

Before this, that question had no answer short of reading the dispatcher. Now it
is one query.

It serves three jobs: debugging, the shadow-mode divergence report, and the
first-party negative signal chains are demoted by. Writes are best-effort and
off the event loop - an unwritable log must never fail an inference call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

# Outcomes a selection can end in.
SUCCESS = "success"
FAILED = "failed"
EXHAUSTED = "exhausted"
# Shadow mode: the old router served the call and the new one disagreed about
# which model should have. Recorded, never acted on.
DIVERGED = "diverged"

# Skips are the useful half of a row, but a chain of 400 models all needing
# billing would write 400 near-identical entries. Keep enough to see the shape.
_MAX_SKIPS_LOGGED = 40


@dataclass(slots=True)
class RoutingDecision:
    """One selection, accumulated during the walk and written when it ends."""

    part: str
    requirements: dict[str, object]
    task_id: str | None = None
    considered: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    chosen_model: str | None = None
    chosen_provider: str | None = None
    outcome: str = EXHAUSTED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def chose(self, model: str, provider: str) -> None:
        self.chosen_model = model
        self.chosen_provider = provider
        self.outcome = SUCCESS


class DecisionLog:
    """Append-only record of routing decisions, in ``models.db``."""

    def __init__(self, db_path: Path, *, enabled: bool = True) -> None:
        self._db_path = db_path
        self._enabled = enabled
        # Writes are offloaded to a worker thread so a decision never sits on the
        # inference path. They are tracked so shutdown can wait for them rather
        # than losing the record of the calls that just ran.
        self._pending: set[asyncio.Future] = set()
        if enabled:
            try:
                self._ensure_schema()
            except Exception:
                logger.warning("Could not open the routing decision log at %s", db_path, exc_info=True)
                self._enabled = False

    def _ensure_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS routing_decisions ("
                "  id            TEXT PRIMARY KEY,"
                "  task_id       TEXT,"
                "  part          TEXT NOT NULL,"
                "  requirements  TEXT NOT NULL,"
                "  considered    INTEGER NOT NULL,"
                "  skipped       TEXT NOT NULL,"
                "  chosen_model  TEXT,"
                "  chosen_provider TEXT,"
                "  outcome       TEXT,"
                "  created_at    TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_task ON routing_decisions (task_id)")

    def record(self, decision: RoutingDecision) -> None:
        """Write one decision, off the event loop and never fatally."""
        if not self._enabled:
            return
        row = (
            decision.id,
            decision.task_id,
            decision.part,
            json.dumps(decision.requirements, default=str),
            decision.considered,
            json.dumps(decision.skipped[:_MAX_SKIPS_LOGGED]),
            decision.chosen_model,
            decision.chosen_provider,
            decision.outcome,
            datetime.now(UTC).isoformat(),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write(row)
            return
        future = loop.run_in_executor(None, self._write, row)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Wait for in-flight writes. Called on shutdown, and before reading back."""
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    def _write(self, row: tuple) -> None:
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO routing_decisions (id, task_id, part, requirements, considered,"
                    " skipped, chosen_model, chosen_provider, outcome, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
        except Exception:
            logger.debug("Could not write a routing decision", exc_info=True)

    def recent(self, *, task_id: str | None = None, part: str | None = None, limit: int = 50) -> list[dict]:
        """Newest decisions first. Powers "why did this run where it ran?"."""
        if not self._enabled:
            return []
        clauses: list[str] = []
        params: list[object] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if part:
            clauses.append("part = ?")
            params.append(part)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM routing_decisions{where} ORDER BY created_at DESC LIMIT ?", params
                ).fetchall()
        except Exception:
            logger.debug("Could not read routing decisions", exc_info=True)
            return []
        return [_row_to_dict(row) for row in rows]

    def failure_rate(self, *, since: timedelta = timedelta(days=7), min_attempts: int = 3) -> dict[str, float]:
        """Per-model share of selections that ended badly - the demotion signal.

        A model this install keeps failing on keeps its place in the chain's tail
        rather than being removed: "worse here" is first-party evidence about
        ranking, not a claim that the model is unusable.
        """
        if not self._enabled:
            return {}
        cutoff = (datetime.now(UTC) - since).isoformat()
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT chosen_model, outcome, COUNT(*) AS n FROM routing_decisions"
                    " WHERE created_at >= ? AND chosen_model IS NOT NULL GROUP BY chosen_model, outcome",
                    (cutoff,),
                ).fetchall()
        except Exception:
            return {}
        totals: dict[str, int] = {}
        failures: dict[str, int] = {}
        for row in rows:
            model = row["chosen_model"]
            totals[model] = totals.get(model, 0) + row["n"]
            if row["outcome"] != SUCCESS:
                failures[model] = failures.get(model, 0) + row["n"]
        return {
            model: failures.get(model, 0) / count
            for model, count in totals.items()
            if count >= min_attempts and failures.get(model, 0)
        }

    def prune(self, older_than: timedelta) -> int:
        """Drop rows past the retention window. Called by the task-cleanup job."""
        if not self._enabled:
            return 0
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        try:
            with open_db_connection(self._db_path) as conn:
                cursor = conn.execute("DELETE FROM routing_decisions WHERE created_at < ?", (cutoff,))
                return cursor.rowcount or 0
        except Exception:
            logger.debug("Could not prune routing decisions", exc_info=True)
            return 0


def _row_to_dict(row) -> dict:
    out = dict(row)
    for key in ("requirements", "skipped"):
        try:
            out[key] = json.loads(out.get(key) or "null")
        except (ValueError, TypeError):
            out[key] = None
    return out
