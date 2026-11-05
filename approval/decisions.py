"""The one table every decision of yours goes to, and how an item is summarised in it.

Every approve, reject or answer is one row in ``decisions`` in approval_memory.db
(#32): `ApprovalMemory` writes it and replays from it. The rejection reason and the
fields you edited are kept with the row.
"""

from __future__ import annotations

from typing import Any

from approval.models import Card
from utils.secrets import redact

# One row per decision: what was decided, by whom, and why. ``fingerprint`` is
# the action's identity for replay ('' when the decision is not about an action);
# ``card_source`` is what produced a prepared-work card ('' for a guard-rail).
_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id       TEXT NOT NULL DEFAULT '',
    fingerprint   TEXT NOT NULL DEFAULT '',
    agent         TEXT NOT NULL,
    signature     TEXT NOT NULL DEFAULT '',
    decision      TEXT NOT NULL,
    chosen_option TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    decided_by    TEXT NOT NULL DEFAULT 'you',
    card_source   TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    edited_fields TEXT NOT NULL DEFAULT '[]',
    decided_at    TEXT NOT NULL
)
"""
_DECISIONS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_decisions_fingerprint ON decisions(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_card_source ON decisions(card_source, decision)",
)


def ensure_decisions_table(conn: Any) -> None:
    """Create the one decisions table, moving rows in from the two it replaced.

    `approval_decisions` (replay by action) and `card_decisions` (prepared work
    and the reason for a rejection) recorded the same thing. Their rows are
    copied across once and the old tables dropped.
    """
    conn.execute(_DECISIONS_SCHEMA)
    for index in _DECISIONS_INDEXES:
        conn.execute(index)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "approval_decisions" in tables:
        conn.execute(
            "INSERT INTO decisions (fingerprint, agent, signature, decision, decided_at) "
            "SELECT fingerprint, agent, signature, decision, updated_at FROM approval_decisions"
        )
        conn.execute("DROP TABLE approval_decisions")
    if "card_decisions" in tables:
        conn.execute(
            "INSERT INTO decisions (card_id, card_source, agent, decision, reason, title, summary, "
            "edited_fields, decided_at) "
            "SELECT card_id, source, agent, decision, reason, title, summary, edited_fields, decided_at "
            "FROM card_decisions"
        )
        conn.execute("DROP TABLE card_decisions")


def card_summary(card: Card) -> str:
    """One string standing for a card, stored with the decision on it.

    Title, message and the values north filled in - what the item *is*, rather
    than how the card was worded. Redacted, because it is stored.
    """
    parts = [card.title, card.message]
    parts.extend(f"{field.display_label()}: {field.value}" for field in card.fields)
    return redact(" \n".join(p for p in parts if p))
