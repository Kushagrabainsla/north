"""Resolving a card produces the next step, rather than only recording a decision.

Issue #14. With a non-blocking card nobody is awaiting the decision, so
approving one has to *start work*. Until this existed, `resolve` wrote a status
and stopped: whatever you decided, the same thing happened next, which was
nothing.

The shape being tested is deliberately not "approve enqueues a job". Reject is a
decision with a next step too, the values come back edited, and a card can offer
more than two options - so the card emits its decision and whatever created it
decides what follows.
"""

from __future__ import annotations

import asyncio

import pytest

from approval.continuation import CardContinuations, CardOutcome
from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.store import ApprovalStore


def _card(**kw) -> Card:
    defaults = {
        "type": CardType.APPROVAL,
        "agent": "general",
        "title": "Application ready",
        "message": "Send it?",
        "source": "job_applications",
        "blocking": False,
        "fields": [CardField(name="company", value="Acme"), CardField(name="cover", value="Dear…", editable=True)],
    }
    defaults.update(kw)
    return Card.new(**defaults)


@pytest.fixture
def wiring():
    continuations = CardContinuations()
    return continuations, ApprovalStore(continuations=continuations)


# ── The next step runs ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approving_starts_the_step_its_creator_registered(wiring) -> None:
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED)
    await continuations.drain()

    assert len(started) == 1
    assert started[0].approved


@pytest.mark.asyncio
async def test_rejecting_also_leads_somewhere(wiring) -> None:
    """The half the first design threw away: a rejection is the more informative signal."""
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.REJECTED)
    await continuations.drain()

    assert started[0].rejected
    assert not started[0].approved


@pytest.mark.asyncio
async def test_a_chosen_option_reaches_the_step(wiring) -> None:
    """"Approve / Reject / Approve without the cover letter" is three next steps."""
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    card = _card(options=["Approve", "Reject", "Approve without cover letter"])
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED, chosen_option="Approve without cover letter")
    await continuations.drain()

    assert started[0].chosen_option == "Approve without cover letter"


@pytest.mark.asyncio
async def test_the_step_runs_on_your_edits_not_the_proposal(wiring) -> None:
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED, values={"cover": "My own words"})
    await continuations.drain()

    assert started[0].values["cover"] == "My own words"
    assert started[0].values["company"] == "Acme", "a non-editable field keeps what north put there"


@pytest.mark.asyncio
async def test_an_expired_card_is_not_a_refusal(wiring) -> None:
    """"Nobody was there" must not be learned from as "you said no"."""
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.TIMEOUT_REJECTED)
    await continuations.drain()

    assert started[0].unanswered
    assert not started[0].rejected


# ── Additive: nothing else changes ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_card_with_no_source_behaves_exactly_as_before(wiring) -> None:
    """The ordinary "may I run this command?" card must not gain a continuation."""
    continuations, store = wiring
    started: list[CardOutcome] = []
    continuations.register("job_applications", lambda outcome: _record(started, outcome))
    guard_rail = Card.new(type=CardType.APPROVAL, agent="coder", title="Run it?", message="rm x")
    store.add(guard_rail)

    assert store.resolve(guard_rail.id, ApprovalDecision.APPROVED) is True
    await continuations.drain()
    assert started == []


@pytest.mark.asyncio
async def test_a_source_with_no_handler_resolves_normally(wiring) -> None:
    _, store = wiring
    card = _card(source="something_unregistered")
    store.add(card)

    assert store.resolve(card.id, ApprovalDecision.APPROVED) is True
    assert store.get(card.id).status == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_a_failing_step_does_not_undo_the_decision(wiring) -> None:
    """A decision is recorded whether or not what follows it works."""
    continuations, store = wiring

    async def _explode(outcome: CardOutcome) -> None:
        raise RuntimeError("the browser died")

    continuations.register("job_applications", _explode)
    card = _card()
    store.add(card)

    assert store.resolve(card.id, ApprovalDecision.APPROVED) is True
    await continuations.drain()
    assert store.get(card.id).status == ApprovalDecision.APPROVED


def test_resolving_without_an_event_loop_still_records(wiring) -> None:
    """A CLI or a test resolves synchronously; the decision must still stand."""
    continuations, store = wiring
    continuations.register("job_applications", lambda outcome: asyncio.sleep(0))
    card = _card()
    store.add(card)

    assert store.resolve(card.id, ApprovalDecision.APPROVED) is True


@pytest.mark.asyncio
async def test_re_registering_replaces_rather_than_accumulates(wiring) -> None:
    """A hot-reloaded flow must not end up with two handlers."""
    continuations, store = wiring
    first: list[CardOutcome] = []
    second: list[CardOutcome] = []
    continuations.register("job_applications", lambda o: _record(first, o))
    continuations.register("job_applications", lambda o: _record(second, o))
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED)
    await continuations.drain()

    assert first == [] and len(second) == 1


async def _record(sink: list[CardOutcome], outcome: CardOutcome) -> None:
    sink.append(outcome)
