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
from approval.models import Card, CardField, CardType
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


# ── Learning from decisions (#17) ────────────────────────────────────────────


async def test_flow_stats_report_how_a_source_is_doing(tmp_path) -> None:
    """A flow you reject 90% of the time is wasting your attention."""
    from approval.decisions import APPROVED, REJECTED, DecisionLog

    log = DecisionLog(tmp_path / "approval_memory.db")
    log.record(_prepared(), APPROVED)
    log.record(_prepared(), REJECTED, reason="too junior")

    with bind_services(ApiServices(decision_log=log)):
        stats = await web_api.flow_stats()

    assert stats[0]["source"] == "job_applications"
    assert stats[0]["approve_rate"] == 0.5
    assert stats[0]["rejection_reasons"] == ["too junior"]


async def test_filtered_candidates_are_visible_and_reversible(tmp_path) -> None:
    """Auto-rejection is only acceptable if you can check what it threw away."""
    from approval.decisions import DecisionLog

    log = DecisionLog(tmp_path / "approval_memory.db")
    log.record_filtered("job_applications", "cand-1", "Junior Developer at Co", "closer to your rejections")

    with bind_services(ApiServices(decision_log=log)):
        assert len(await web_api.filtered_candidates()) == 1
        await web_api.unfilter_candidate("cand-1")
        assert await web_api.filtered_candidates() == []
