"""A card carrying work must not carry a credential into the audit log.

Cards now hold values north filled in, and those are written to SQLite and
audit-logged permanently - a filled-in form is exactly where an API key or a
password ends up. `memory/facts.py` already refuses to store a fact containing
one; a card cannot refuse, because it is the thing the user has to read, so it
redacts instead.

Scrubbing happens on the way into the queue rather than on the way to disk: the
queue is read by the web UI and the notifiers too, so redacting only at the
database would still hand a credential to Telegram.
"""

from __future__ import annotations

from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.store import ApprovalStore
from utils.secrets import REDACTED


def _card(**kw) -> Card:
    defaults = {"type": CardType.APPROVAL, "agent": "general", "title": "t", "message": "m"}
    defaults.update(kw)
    return Card.new(**defaults)


def test_a_field_named_like_a_secret_is_redacted_whatever_its_value() -> None:
    """A password of "hunter2" matches no pattern. The label is the evidence."""
    card = _card(fields=[CardField(name="password", value="hunter2")])
    assert card.scrubbed().fields[0].value == REDACTED


def test_a_value_that_looks_like_a_secret_is_redacted_under_any_name() -> None:
    card = _card(fields=[CardField(name="notes", value="api_key = sk_live_abcdef123456")])
    assert REDACTED in str(card.scrubbed().fields[0].value)


def test_the_work_itself_survives() -> None:
    """Redaction must not make the card unreviewable - that is the whole point of it."""
    card = _card(fields=[CardField(name="company", value="Acme"), CardField(name="token", value="x")])
    scrubbed = card.scrubbed()
    assert scrubbed.fields[0].value == "Acme"
    assert scrubbed.fields[1].value == REDACTED


def test_a_clean_card_is_returned_unchanged() -> None:
    """Every card passes through here; the common case must stay free."""
    card = _card(fields=[CardField(name="company", value="Acme")])
    assert card.scrubbed() is card


def test_nothing_secret_reaches_the_store(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    card = _card(fields=[CardField(name="api_key", value="sk_live_abcdef123456", editable=True)])
    store.add(card)

    assert store.get(card.id).fields[0].value == REDACTED


def test_nothing_secret_reaches_the_store_on_resolve(tmp_path) -> None:
    """The user can paste one into an editable field too."""
    store = ApprovalStore(tmp_path / "approvals.db")
    card = _card(fields=[CardField(name="notes", value="", editable=True)])
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED, values={"notes": "password: hunter2000000"})

    assert REDACTED in str(store.get(card.id).response["notes"])


def test_a_secret_in_the_message_or_context_is_redacted() -> None:
    card = _card(message="run with api_key = abcdef123456", context="token: ghp_abcdefghijkl")
    scrubbed = card.scrubbed()
    assert "abcdef123456" not in scrubbed.message
    assert "ghp_abcdefghijkl" not in scrubbed.context
