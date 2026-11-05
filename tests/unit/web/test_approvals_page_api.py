"""What the review page needs from the API to let you judge an item.

Issue #15, after the premise was corrected: reviewing does not need to be fast.
The page is where something goes out in your name, so optimising for throughput
optimises for the failure it exists to prevent. What it needs instead is enough
information to decide well - which cards are blocking something, and what
approving one will actually cause.
"""

from __future__ import annotations

import pytest

from approval.continuation import CardContinuations
from approval.interaction import UserInteraction
from approval.models import Card, CardField, CardType, DecidedBy, MemoryKind, MemoryRef
from approval.policy import Answer
from approval.store import ApprovalStore
from orchestrator.api_context import ApiServices, bind_services
from web import api as web_api


def _prepared() -> Card:
    return Card.new(
        type=CardType.APPROVAL,
        agent="general",
        title="Application ready",
        message="Send it?",
        source="job_applications",
        blocking=False,
        context="We are hiring a Staff Engineer…",
        fields=[CardField(name="company", value="Acme")],
    )


def _guard_rail() -> Card:
    return Card.new(type=CardType.APPROVAL, agent="coder", title="Run migration?", message="drops a column")


@pytest.fixture
def wiring(tmp_path):
    continuations = CardContinuations()
    store = ApprovalStore(tmp_path / "approvals.db", continuations=continuations)
    with bind_services(ApiServices(approval_store=store, card_continuations=continuations)):
        yield continuations, store


async def test_the_page_can_tell_the_two_kinds_apart(wiring) -> None:
    """An agent frozen mid-action and work that can wait have different costs of being missed."""
    _, store = wiring
    store.add(_prepared())
    store.add(_guard_rail())

    by_title = {c["title"]: c for c in await web_api.approvals()}

    assert by_title["Application ready"]["blocking"] is False
    assert by_title["Run migration?"]["blocking"] is True


async def test_legacy_information_cards_are_not_actionable(wiring) -> None:
    """Old persisted completion notices must stay out of the decision cockpit."""
    _, store = wiring
    notification = Card.new(
        type=CardType.INFORMATION,
        agent="research",
        title="Research - Done",
        message="Ready.",
    )
    # Simulate an old database restored before the store enforced the boundary.
    store._cards[notification.id] = notification  # noqa: SLF001

    assert await web_api.approvals() == []


async def test_a_card_says_what_approving_will_cause(wiring) -> None:
    """ "Submits the application" and "saves a draft" must not be identical buttons."""
    continuations, store = wiring
    continuations.register("job_applications", _noop, describes="submit the application")
    store.add(_prepared())

    card = (await web_api.approvals())[0]

    assert card["next_step"] == "submit the application"


async def test_a_card_with_no_registered_step_claims_nothing(wiring) -> None:
    """Better to say nothing than to imply a consequence that will not happen."""
    _, store = wiring
    store.add(_guard_rail())

    assert (await web_api.approvals())[0]["next_step"] == ""


async def test_the_source_material_travels_with_the_work(wiring) -> None:
    """You cannot judge a filled-in application without the posting behind it."""
    _, store = wiring
    store.add(_prepared())

    assert "Staff Engineer" in (await web_api.approvals())[0]["context"]


async def test_no_endpoint_decides_more_than_one_card(wiring) -> None:
    """There is deliberately no bulk approve: it is the fastest review and the worst.

    Pinned as a test because it will read as an obvious missing feature to
    whoever maintains this page next.
    """
    paths = {route.path for route in web_api.router.routes}
    assert not any("approve-all" in p or "approve_all" in p for p in paths)


async def _noop(outcome) -> None:
    return None


async def test_every_card_says_who_decided_it_in_words(wiring) -> None:
    """#30: the page, TUI and Telegram all show the same label from here."""
    _, store = wiring
    decided = _guard_rail()
    store.add(decided)
    store.resolve(decided.id, "rejected", decided_by=DecidedBy.MEMORY_DECIDER)
    store.overrule(decided.id, "approved", chosen_option="Approve")
    store.add(_prepared())

    by_title = {c["title"]: c for c in await web_api.approvals()}

    assert by_title["Run migration?"]["decided_by_label"] == "you"
    assert by_title["Run migration?"]["overruled"]["decided_by_label"] == "the memory decider"
    assert by_title["Application ready"]["decided_by_label"] == ""


async def test_each_memory_a_decision_used_is_named_in_words(wiring) -> None:
    _, store = wiring
    used = (MemoryRef(kind=MemoryKind.PAST_DECISION, text="you approved 'x'", ref="fp1"),)
    answer = Answer("approved", "Approve", "you always allow this", DecidedBy.MEMORY_DECIDER, used)
    UserInteraction(store).record_resolved(_guard_rail(), answer)

    [card] = await web_api.approvals()

    assert card["decided_by_label"] == "the memory decider"
    assert card["memory_used"] == [
        {"kind": "past_decision", "text": "you approved 'x'", "ref": "fp1", "label": "Your past decision"}
    ]
