"""Learned approval memory - remembers how the user decided past approval cards.

This is the "based on previous data" substrate for autonomous mode: every time the
user approves or rejects an action, the decision is recorded against a fingerprint
of that action (the agent plus a normalized signature of the command / edit). When
a matching action comes up again, north can replay the user's own prior decision
instead of asking - so the more you use it, the less it interrupts you.

Persisted in the one ``decisions`` table (#32), which this class writes: every
decision of yours is one row, with the card it came from and your reason, so
`DecisionLog` learns from the same rows the replay reads. Cached in memory for
fast lookups on the approval hot path. Only *your* decisions are replayed; a
decision north took for you is derived, not new signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from approval.decisions import card_summary, ensure_decisions_table
from approval.models import ApprovalDecision, DecidedBy
from utils.db import open_db_connection
from utils.secrets import redact

if TYPE_CHECKING:
    from approval.models import Card

logger = logging.getLogger(__name__)

_REPLAYABLE = (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)
# Your latest approve/reject for each action: what replay answers with.
_LATEST_REPLAYABLE = (
    "SELECT MAX(id) AS id, COUNT(*) AS n FROM decisions "
    "WHERE fingerprint != '' AND decided_by = 'you' AND decision IN ('approved', 'rejected') "
    "GROUP BY fingerprint"
)

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Marks the one-time removal of decisions keyed by the old prefix fingerprint.
_PREFIX_PURGE_KEY = "prefix_fingerprints_dropped"


def _drop_prefix_fingerprints(conn: Any) -> None:
    """Delete decisions recorded under the 80-character prefix fingerprint, once.

    Those rows cannot be re-keyed: the fingerprint is a hash, and the full text
    it should have covered was never stored. Leaving them would be worse than
    dropping them - they would sit in the cockpit looking like remembered
    decisions while never matching anything again, and any one of them might be
    a collision that recorded a verdict the user never gave for that action.

    So they go, loudly. The cost is being asked again about actions already
    decided; the alternative is trusting verdicts that may not be theirs.
    """
    if conn.execute("SELECT 1 FROM approval_memory_meta WHERE key = ?", (_PREFIX_PURGE_KEY,)).fetchone():
        return
    dropped = conn.execute("DELETE FROM approval_decisions").rowcount if _has_old_table(conn) else 0
    conn.execute("INSERT INTO approval_memory_meta(key, value) VALUES (?, '1')", (_PREFIX_PURGE_KEY,))
    if dropped:
        logger.info(
            "ApprovalMemory: dropped %d learned decision(s) keyed by the old 80-character prefix "
            "fingerprint - it could not tell two actions with a long shared prefix apart. "
            "north will ask about those actions again.",
            dropped,
        )


# Marks the one-time removal of decisions keyed by the card's message text.
_MESSAGE_KEY_PURGE_KEY = "message_keyed_decisions_dropped"


def _drop_message_keyed_decisions(conn: Any) -> None:
    """Delete decisions recorded under the card's message text, once.

    Since 64789a2 the policy recalls a decision by the action's identity
    (`Action.describe()`), while the orchestrator kept recording it by the
    card's message - so no decision recorded since has ever been replayed, and
    none recorded before it matches the new key either. The message behind each
    fingerprint was never stored in full, so the rows cannot be re-keyed; they
    would only sit on the Memory page looking learned while never firing.
    """
    if conn.execute("SELECT 1 FROM approval_memory_meta WHERE key = ?", (_MESSAGE_KEY_PURGE_KEY,)).fetchone():
        return
    dropped = conn.execute("DELETE FROM approval_decisions").rowcount if _has_old_table(conn) else 0
    conn.execute("INSERT INTO approval_memory_meta(key, value) VALUES (?, '1')", (_MESSAGE_KEY_PURGE_KEY,))
    if dropped:
        logger.info(
            "ApprovalMemory: dropped %d learned decision(s) keyed by the card's message text - "
            "they were never matched by the policy's action key. north will ask about those "
            "actions again and remember the answer correctly.",
            dropped,
        )


def _has_old_table(conn: Any) -> bool:
    """Whether the pre-#32 replay table is still here, not yet merged into `decisions`."""
    return bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'approval_decisions'").fetchone()
    )


_FENCE_RE = re.compile(r"```[a-z]*")
_WS_RE = re.compile(r"\s+")
# How much of the action text is shown as its label in the cockpit. Display
# only - identity is the whole message (see `_fingerprint`).
_DISPLAY_SIGNATURE_CHARS = 80


def _normalize(message: str) -> str:
    """The action text with formatting noise removed. Not truncated."""
    text = _FENCE_RE.sub(" ", message or "")
    text = text.replace("`", " ").strip().lower()
    return _WS_RE.sub(" ", text)


def _display_signature(message: str) -> str:
    """A short label for the cockpit's list of remembered decisions."""
    return _normalize(message)[:_DISPLAY_SIGNATURE_CHARS]


def _fingerprint(agent: str, message: str) -> str:
    """Identity of an action, over its *whole* normalized text.

    This used to hash only the first 80 characters, which made any two actions
    sharing a long prefix the same action. A `cd`-and-activate preamble is
    routinely longer than that, so "run the tests" and "delete the data
    directory" could carry one fingerprint - and approving the first taught
    north to replay that approval for the second.

    Hashing everything makes a fingerprint narrower, not wider: a volatile tail
    (a diff hunk, a commit message) now makes an action look unique, so north
    asks again instead of replaying. That is the safe direction to be wrong in -
    an extra question costs a moment, a false match acts without being asked.
    """
    return hashlib.sha256(f"{agent}::{_normalize(message)}".encode()).hexdigest()


class ApprovalMemory:
    """Records every decision of yours, and recalls your past approve/reject by action fingerprint."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_META_SCHEMA)
            # The one-time purges run on the old table, before its rows move.
            _drop_prefix_fingerprints(conn)
            _drop_message_keyed_decisions(conn)
            ensure_decisions_table(conn)
        # fingerprint -> decision, loaded lazily and kept in sync on record().
        self._cache: dict[str, str] | None = None

    def _ensure_cache(self) -> dict[str, str]:
        if self._cache is None:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    f"SELECT d.fingerprint, d.decision FROM decisions d JOIN ({_LATEST_REPLAYABLE}) l ON d.id = l.id"
                ).fetchall()
            self._cache = {r["fingerprint"]: r["decision"] for r in rows}
        return self._cache

    def recall(self, agent: str, message: str) -> str | None:
        """Return the user's prior decision ('approved'/'rejected') for this action, or None."""
        return self._ensure_cache().get(_fingerprint(agent, message))

    def all_decisions(self) -> list[dict[str, object]]:
        """Every action north replays a decision for, with your latest answer, newest first.

        Autonomous mode replays these instead of asking, so they have to be
        readable: a rule you cannot see is one you cannot check before trusting
        it. ``signature`` is the normalized action the fingerprint was taken
        from, which is what makes a row mean something to a person; ``count`` is
        how many times you decided it.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT d.fingerprint, d.agent, d.signature, d.decision, l.n AS count, d.decided_at AS updated_at "
                    f"FROM decisions d JOIN ({_LATEST_REPLAYABLE}) l ON d.id = l.id ORDER BY d.id DESC"
                ).fetchall()
        except Exception:
            logger.warning("ApprovalMemory: failed to read decisions", exc_info=True)
            return []
        return [dict(row) for row in rows]

    def forget(self, fingerprint: str) -> bool:
        """Stop replaying one action's decision. True when there was one.

        The counterpart to recording. A decision that cannot be withdrawn is not
        consent. The rows stay - they are still what you decided, and flows
        learn from them - but they no longer carry the action's fingerprint, so
        north asks about that action again.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                removed = conn.execute(
                    "UPDATE decisions SET fingerprint = '' WHERE fingerprint = ?", (fingerprint,)
                ).rowcount
        except Exception:
            logger.warning("ApprovalMemory: failed to forget %s", fingerprint, exc_info=True)
            return False
        if removed:
            self._ensure_cache().pop(fingerprint, None)
        return bool(removed)

    def record(
        self,
        agent: str,
        message: str,
        decision: str,
        *,
        card: Card | None = None,
        chosen_option: str = "",
        reason: str = "",
        edited_fields: list[str] | None = None,
        decided_by: str = DecidedBy.YOU,
    ) -> None:
        """Persist one decision as one row. The latest approve/reject for an action is what replays.

        *message* is the action's identity (`Action.describe()`); "" when the
        decision is not about an action, such as an answered question, which is
        then recorded but never replayed.
        """
        fp = _fingerprint(agent, message) if message else ""
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO decisions (card_id, fingerprint, agent, signature, decision, chosen_option, reason, "
                    "decided_by, card_source, title, summary, edited_fields, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        card.id if card else "",
                        fp,
                        agent,
                        _display_signature(message),
                        decision,
                        chosen_option,
                        redact(reason),
                        decided_by,
                        card.source if card else "",
                        card.title if card else "",
                        card_summary(card) if card and card.source else "",
                        json.dumps(edited_fields or []),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except Exception:
            # Never fail a decision because recording it failed. The decision is
            # what the user asked for; this is bookkeeping about it.
            logger.warning("ApprovalMemory: failed to record decision for agent %s", agent, exc_info=True)
            return
        if fp and decided_by == DecidedBy.YOU and decision in _REPLAYABLE:
            self._ensure_cache()[fp] = decision
