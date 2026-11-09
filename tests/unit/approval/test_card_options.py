"""What a card's options mean is declared when it is made, and a waiting card is never trimmed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.notices import NoticeStore
from approval.store import _MAX_RESOLVED, ApprovalStore
from orchestrator.orchestrator import _decision_from_reply


def _card(**kw) -> Card:
    defaults = {"type": CardType.APPROVAL, "agent": "a", "title": "t", "message": "m"}
    return Card.new(**{**defaults, **kw})


@pytest.mark.parametrize("option", ["Discard", "Don't send", "Stop", "Not now"])
def test_an_option_declared_as_no_is_recorded_as_no(option: str) -> None:
    card = _card(options=["Go", option], rejecting=[option])

    assert _decision_from_reply(card, option, "") == (ApprovalDecision.REJECTED, option)


def test_an_option_not_declared_as_no_is_a_yes_whatever_it_says() -> None:
    card = _card(options=["Cancel the subscription", "Keep it"], rejecting=["Keep it"])

    assert _decision_from_reply(card, "Cancel the subscription", "")[0] == ApprovalDecision.APPROVED


def test_plain_cards_declare_reject_for_themselves() -> None:
    assert _card(options=["Approve", "Reject"]).rejecting == ["Reject"]


def test_a_bare_approve_on_a_card_with_its_own_options_is_refused() -> None:
    """A dashboard Approve on a Resume/Discard card closed it and resumed nothing."""
    card = _card(options=["Resume", "Discard"], rejecting=["Discard"])

    with pytest.raises(ValueError, match="own options"):
        _decision_from_reply(card, "approved", "")

    assert _decision_from_reply(card, "approved", "Resume") == ("approved", "Resume")


def test_a_rejection_with_a_reason_is_still_a_rejection() -> None:
    card = _card(options=["Write", "Cancel"], rejecting=["Cancel"])

    assert _decision_from_reply(card, "rejected", "Reject: too long") == ("rejected", "Reject: too long")


def test_an_options_number_picks_it_on_a_one_question_card() -> None:
    field = CardField(name="q1", label="Which?", value="", editable=True, options=["Once", "Recurring"])
    card = _card(type=CardType.QUESTION, fields=[field])

    assert card.answers_in("2") == {"q1": "Recurring"}


def _resolved_past_the_cap(store: ApprovalStore, older_than: Card) -> None:
    for _ in range(_MAX_RESOLVED):
        card = _card()
        store.add(card)
        store.resolve(card.id, ApprovalDecision.APPROVED)


def test_a_late_answer_on_an_old_card_is_not_trimmed_at_once(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    old = _card().model_copy(update={"created_at": datetime.now(UTC) - timedelta(days=90)})
    store.add(old)
    _resolved_past_the_cap(store, old)

    assert store.resolve(old.id, ApprovalDecision.APPROVED)

    assert store.get(old.id) is not None and store.get(old.id).status == "approved"


def test_a_waiting_card_survives_a_restart_however_much_history_came_after(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ApprovalStore(db)
    waiting = _card(source="prepared", blocking=False)
    waiting = waiting.model_copy(update={"created_at": datetime.now(UTC) - timedelta(days=90)})
    store.add(waiting)
    _resolved_past_the_cap(store, waiting)

    assert [card.id for card in ApprovalStore(db).pending()] == [waiting.id]


def test_a_waiting_card_is_listed_however_many_are_newer(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    waiting = _card(source="prepared", blocking=False)
    waiting = waiting.model_copy(update={"created_at": datetime.now(UTC) - timedelta(days=90)})
    store.add(waiting)
    for _ in range(5):
        card = _card()
        store.add(card)
        store.resolve(card.id, ApprovalDecision.APPROVED)

    listed = store.all(limit=2)

    assert waiting.id in [card.id for card in listed] and len(listed) == 3


async def test_a_reminder_is_kept_where_the_dashboard_shows_it(tmp_path: Path) -> None:
    store, notices = ApprovalStore(tmp_path / "a.db"), NoticeStore()
    store.add(_card(source="prepared", blocking=False))

    await UserInteraction(store, notices=notices).remind_waiting()

    assert [notice.title for notice in notices.waiting()] == ["1 waiting for you"]


async def test_a_problem_with_a_key_is_told_once_while_it_lasts(tmp_path: Path) -> None:
    notices = NoticeStore(tmp_path / "n.db")
    interaction = UserInteraction(ApprovalStore(tmp_path / "a.db"), notices=notices)

    for _ in range(3):
        await interaction.deliver(title="t", message="m", source="s", key="flow-start:x")
    assert len(notices.waiting()) == 1

    interaction.clear_notice("flow-start:x")
    assert notices.waiting() == []

    await interaction.deliver(title="t", message="m", source="s", key="flow-start:x")
    assert len(notices.waiting()) == 1


def test_an_older_notices_table_gains_the_key_column(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "n.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE notices (id TEXT PRIMARY KEY, title TEXT NOT NULL, message TEXT NOT NULL, "
            "source TEXT NOT NULL, task_id TEXT, created_at TEXT NOT NULL, dismissed_at TEXT)"
        )

    notices = NoticeStore(db)
    notices.add(title="t", message="m", source="s", key="k")

    assert notices.open_with("k") is not None
