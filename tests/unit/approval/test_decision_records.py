"""What a decision on prepared work keeps: your reason, the item, and never a secret.

Issue #17. A rejection without its reason says only "no", so the reason is stored
with the decision, redacted like everything else north writes down.
"""

from __future__ import annotations

import pytest

from approval.approval_memory import ApprovalMemory
from approval.decisions import card_summary
from approval.models import Card, CardField, CardType
from utils.db import open_db_connection


@pytest.fixture
def db(tmp_path):
    return tmp_path / "approval_memory.db"


def _card(title: str = "Staff Engineer at Acme", source: str = "job_applications", **kw) -> Card:
    defaults = {
        "type": CardType.APPROVAL,
        "agent": "general",
        "title": title,
        "message": "Apply?",
        "source": source,
        "blocking": False,
        "fields": [CardField(name="company", value="Acme")],
    }
    defaults.update(kw)
    return Card.new(**defaults)


def _reasons(db) -> list[str]:
    with open_db_connection(db) as conn:
        return [row["reason"] for row in conn.execute("SELECT reason FROM decisions WHERE card_source != ''")]


def test_a_rejection_keeps_its_reason(db) -> None:
    """Without one a rejection says only "no"."""
    card = _card()
    ApprovalMemory(db).record(card.agent, "", "rejected", card=card, reason="Not senior enough")
    assert _reasons(db) == ["Not senior enough"]


def test_a_secret_in_a_reason_is_not_stored(db) -> None:
    card = _card()
    ApprovalMemory(db).record(
        card.agent, "", "rejected", card=card, reason="wrong account, api_key = sk_live_abcdef123456"
    )
    assert "sk_live_abcdef123456" not in _reasons(db)[0]


def test_a_card_summary_is_what_the_item_is() -> None:
    """Summarised on the item, not on how the card happened to be worded."""
    summary = card_summary(_card())
    assert "Staff Engineer at Acme" in summary and "Acme" in summary
