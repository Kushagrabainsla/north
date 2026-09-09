"""Learned approval memory - remembers how the user decided past approval cards.

This is the "based on previous data" substrate for autonomous mode: every time the
user approves or rejects an action, the decision is recorded against a fingerprint
of that action (the agent plus a normalized signature of the command / edit). When
a matching action comes up again, north can replay the user's own prior decision
instead of asking - so the more you use it, the less it interrupts you.

Persisted in SQLite and cached in memory for fast lookups on the approval hot path.
Only *human* decisions are recorded (auto-decisions are derived, not new signal).
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_decisions (
    fingerprint TEXT     NOT NULL PRIMARY KEY,
    agent       TEXT     NOT NULL,
    signature   TEXT     NOT NULL,
    decision    TEXT     NOT NULL,
    count       INTEGER  NOT NULL DEFAULT 1,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

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
    dropped = conn.execute("DELETE FROM approval_decisions").rowcount
    conn.execute("INSERT INTO approval_memory_meta(key, value) VALUES (?, '1')", (_PREFIX_PURGE_KEY,))
    if dropped:
        logger.info(
            "ApprovalMemory: dropped %d learned decision(s) keyed by the old 80-character prefix "
            "fingerprint - it could not tell two actions with a long shared prefix apart. "
            "north will ask about those actions again.",
            dropped,
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
    """Records and recalls the user's past approval decisions by action fingerprint."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.execute(_META_SCHEMA)
            _drop_prefix_fingerprints(conn)
        # fingerprint -> decision, loaded lazily and kept in sync on record().
        self._cache: dict[str, str] | None = None

    def _ensure_cache(self) -> dict[str, str]:
        if self._cache is None:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT fingerprint, decision FROM approval_decisions").fetchall()
            self._cache = {r["fingerprint"]: r["decision"] for r in rows}
        return self._cache

    def recall(self, agent: str, message: str) -> str | None:
        """Return the user's prior decision ('approved'/'rejected') for this action, or None."""
        return self._ensure_cache().get(_fingerprint(agent, message))

    def all_decisions(self) -> list[dict[str, object]]:
        """Every decision north has learned, newest first.

        Autonomous mode replays these instead of asking, so they have to be
        readable: a rule you cannot see is one you cannot check before trusting
        it. ``signature`` is the normalized action the fingerprint was taken
        from, which is what makes a row mean something to a person.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT fingerprint, agent, signature, decision, count, updated_at "
                    "FROM approval_decisions ORDER BY updated_at DESC"
                ).fetchall()
        except Exception:
            logger.warning("ApprovalMemory: failed to read decisions", exc_info=True)
            return []
        return [dict(row) for row in rows]

    def forget(self, fingerprint: str) -> bool:
        """Drop one learned decision. True when a row was removed.

        The counterpart to recording. A decision that cannot be withdrawn is not
        consent, and until now the only way to take one back was to delete the
        database.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                removed = conn.execute("DELETE FROM approval_decisions WHERE fingerprint = ?", (fingerprint,)).rowcount
        except Exception:
            logger.warning("ApprovalMemory: failed to forget %s", fingerprint, exc_info=True)
            return False
        if removed:
            self._ensure_cache().pop(fingerprint, None)
        return bool(removed)

    def record(self, agent: str, message: str, decision: str) -> None:
        """Persist a human decision. The latest decision for a fingerprint wins."""
        if decision not in ("approved", "rejected"):
            return  # only learn from clear approve/reject signals
        fp = _fingerprint(agent, message)
        sig = _display_signature(message)
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO approval_decisions (fingerprint, agent, signature, decision, count) "
                    "VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET "
                    "decision=excluded.decision, "
                    "count=approval_decisions.count + 1, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (fp, agent, sig, decision),
                )
        except Exception:
            logger.warning("ApprovalMemory: failed to record decision for agent %s", agent, exc_info=True)
            return
        self._ensure_cache()[fp] = decision
