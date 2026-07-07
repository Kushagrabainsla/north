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

_FENCE_RE = re.compile(r"```[a-z]*")
_WS_RE = re.compile(r"\s+")
# How much of the normalized action text defines its identity. Enough to tell
# "npm install x" from "pytest", short enough that volatile tails (a commit
# message, a specific diff hunk) don't make every action look unique.
_SIGNATURE_CHARS = 80


def _normalize(message: str) -> str:
    text = _FENCE_RE.sub(" ", message or "")
    text = text.replace("`", " ").strip().lower()
    return _WS_RE.sub(" ", text)[:_SIGNATURE_CHARS]


def _fingerprint(agent: str, message: str) -> str:
    sig = _normalize(message)
    return hashlib.sha256(f"{agent}::{sig}".encode()).hexdigest()


class ApprovalMemory:
    """Records and recalls the user's past approval decisions by action fingerprint."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
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

    def record(self, agent: str, message: str, decision: str) -> None:
        """Persist a human decision. The latest decision for a fingerprint wins."""
        if decision not in ("approved", "rejected"):
            return  # only learn from clear approve/reject signals
        fp = _fingerprint(agent, message)
        sig = _normalize(message)
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
