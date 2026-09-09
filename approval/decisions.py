"""What you decided, and why - the best training signal north has.

Every time you approve or reject prepared work you produce a labelled example of
your own taste, on your own real data, for free. It is higher quality than fact
extraction or episodic summarisation, both of which north already invests
heavily in. Until now it was written to a `status` column and forgotten, so a
workflow could never get better - it would keep producing the same proportion of
things you reject.

`skills/retirement.py` already closes exactly this loop for skills: one that
keeps appearing in failed tasks is retired. This is the equivalent for prepared
work.

Two things make the signal usable rather than merely recorded:

**A rejection carries a reason.** Without one a rejection says only "no", which
cannot be learned from. Chips defined per source, plus free text.

**An expiry is not a rejection.** Nobody was there. Collapsing the two teaches a
flow that "you were asleep" means "you said no", which is how a filter learns to
hide things you would have wanted.

Stored in approval_memory.db beside the learned decisions and the safe-action
rules - all three answer questions about how you decide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from approval.models import Card
from utils.db import open_db_connection
from utils.secrets import redact

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS card_decisions (
    card_id       TEXT NOT NULL PRIMARY KEY,
    source        TEXT NOT NULL,
    agent         TEXT NOT NULL,
    decision      TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    edited_fields TEXT NOT NULL DEFAULT '[]',
    decided_at    DATETIME NOT NULL
)
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_card_decisions_source ON card_decisions(source, decision)"

_FILTERED_SCHEMA = """
CREATE TABLE IF NOT EXISTS filtered_candidates (
    id          TEXT NOT NULL PRIMARY KEY,
    source      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    filtered_at DATETIME NOT NULL
)
"""

APPROVED = "approved"
REJECTED = "rejected"
# Deliberately separate from REJECTED everywhere below.
UNANSWERED = "timeout_rejected"


@dataclass(frozen=True)
class SourceStats:
    """How a source is doing at proposing things you want."""

    source: str
    offered: int
    approved: int
    rejected: int
    unanswered: int

    @property
    def decided(self) -> int:
        """Cards you actually answered. An expiry is not a data point about taste."""
        return self.approved + self.rejected

    @property
    def approve_rate(self) -> float:
        """Of the ones you answered, the share you approved. 0.0 when none."""
        return self.approved / self.decided if self.decided else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "offered": self.offered,
            "approved": self.approved,
            "rejected": self.rejected,
            "unanswered": self.unanswered,
            "decided": self.decided,
            "approve_rate": round(self.approve_rate, 3),
        }


def card_summary(card: Card) -> str:
    """One string standing for a card, for comparing it against past decisions.

    Title, message and the values north filled in - what the item *is*, rather
    than how the card was worded. Redacted, because these are stored and later
    embedded.
    """
    parts = [card.title, card.message]
    parts.extend(f"{field.display_label()}: {field.value}" for field in card.fields)
    return redact(" \n".join(p for p in parts if p))


class DecisionLog:
    """Records how prepared work was decided, and reports how a source is doing."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.execute(_FILTERED_SCHEMA)

    def record(self, card: Card, decision: str, reason: str = "", edited_fields: list[str] | None = None) -> None:
        """Log one decision. Only cards with a source: a guard-rail teaches nothing.

        A guard-rail card is a question about one action in one task, not an
        example of what you want proposed, so recording it would dilute the
        signal with data that cannot inform any flow.
        """
        if not card.source:
            return
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO card_decisions "
                    "(card_id, source, agent, decision, reason, title, summary, edited_fields, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        card.id,
                        card.source,
                        card.agent,
                        decision,
                        redact(reason),
                        card.title,
                        card_summary(card),
                        json.dumps(edited_fields or []),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except Exception:
            # Never fail a decision because logging it failed. The decision is
            # what the user asked for; this is bookkeeping about it.
            logger.warning("DecisionLog: could not record the decision for card %s", card.id, exc_info=True)

    # ── Reading it back ──────────────────────────────────────────────────────

    def summaries(self, source: str, decision: str, limit: int = 200) -> list[str]:
        """What past items decided this way looked like, newest first."""
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT summary FROM card_decisions WHERE source = ? AND decision = ? AND summary != '' "
                "ORDER BY decided_at DESC LIMIT ?",
                (source, decision, limit),
            ).fetchall()
        return [row["summary"] for row in rows]

    def rejection_reasons(self, source: str, limit: int = 200) -> list[str]:
        """Why you said no, in your words - the input to rewriting a find prompt."""
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT reason FROM card_decisions WHERE source = ? AND decision = ? AND reason != '' "
                "ORDER BY decided_at DESC LIMIT ?",
                (source, REJECTED, limit),
            ).fetchall()
        return [row["reason"] for row in rows]

    def stats(self, source: str) -> SourceStats:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT decision, COUNT(*) AS n FROM card_decisions WHERE source = ? GROUP BY decision",
                (source,),
            ).fetchall()
        counts = {row["decision"]: row["n"] for row in rows}
        return SourceStats(
            source=source,
            offered=sum(counts.values()),
            approved=counts.get(APPROVED, 0),
            rejected=counts.get(REJECTED, 0),
            unanswered=counts.get(UNANSWERED, 0),
        )

    def all_stats(self) -> list[SourceStats]:
        """Every source's record. A flow you reject 90% of the time is wasting
        your attention, and nothing would currently notice."""
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT DISTINCT source FROM card_decisions").fetchall()
        return sorted((self.stats(row["source"]) for row in rows), key=lambda s: s.approve_rate)

    def most_edited_fields(self, source: str, limit: int = 5) -> list[tuple[str, int]]:
        """Which fields you rewrite, and how often.

        If you rewrite the cover letter every single time, that is training data
        for the prompt that drafts it - the diff is the correction.
        """
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT edited_fields FROM card_decisions WHERE source = ? AND decision = ?",
                (source, APPROVED),
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            try:
                for name in json.loads(row["edited_fields"]):
                    counts[name] = counts.get(name, 0) + 1
            except Exception:
                continue
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    # ── Auto-rejection, kept auditable ───────────────────────────────────────

    def record_filtered(self, source: str, candidate_id: str, summary: str, reason: str) -> None:
        """Note a candidate dropped before it ever became a card.

        Auto-rejection is only acceptable if you can check what it threw away -
        and it is also how you find out the threshold is wrong.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO filtered_candidates (id, source, summary, reason, filtered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, source, redact(summary), reason, datetime.now(UTC).isoformat()),
                )
        except Exception:
            logger.warning("DecisionLog: could not record a filtered candidate", exc_info=True)

    def filtered(self, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with open_db_connection(self._db_path) as conn:
            if source:
                rows = conn.execute(
                    "SELECT * FROM filtered_candidates WHERE source = ? ORDER BY filtered_at DESC LIMIT ?",
                    (source, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM filtered_candidates ORDER BY filtered_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def unfilter(self, candidate_id: str) -> bool:
        """Undo one auto-rejection, so the same item can be offered again."""
        with open_db_connection(self._db_path) as conn:
            removed = conn.execute("DELETE FROM filtered_candidates WHERE id = ?", (candidate_id,)).rowcount
        return bool(removed)
