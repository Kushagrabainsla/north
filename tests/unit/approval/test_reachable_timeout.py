"""How long a blocking card waits, given where it can be answered.

The 300s default assumes someone at a prompt. A card raised at 03:00 by a
schedule expires long before anyone sees it, and an expiry denies the action -
so fixing delivery (#2) without this would only convert an invisible failure
into a silent denial, which is worse because it looks like it worked.
"""

from __future__ import annotations

import pytest

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardType
from approval.store import ApprovalStore


def _interaction(**kw) -> UserInteraction:
    return UserInteraction(ApprovalStore(), **kw)


def _card() -> Card:
    return Card.new(type=CardType.APPROVAL, agent="coder", title="t", message="m")


def test_unreachable_keeps_the_short_default() -> None:
    """With nowhere to send it, waiting longer just holds a task slot to reach the same denial."""
    interaction = _interaction(default_timeout=300.0, reachable=lambda: False)
    assert interaction._timeout_for(_card()) == 300.0


def test_reachable_waits_long_enough_to_cover_a_night() -> None:
    interaction = _interaction(default_timeout=300.0, reachable_timeout=43_200.0, reachable=lambda: True)
    assert interaction._timeout_for(_card()) == 43_200.0


def test_reachability_is_read_per_card_not_at_construction() -> None:
    """Turning Telegram on should change the next card's wait without a restart."""
    reachable = False
    interaction = _interaction(default_timeout=300.0, reachable_timeout=43_200.0, reachable=lambda: reachable)

    assert interaction._timeout_for(_card()) == 300.0
    reachable = True
    assert interaction._timeout_for(_card()) == 43_200.0


def test_the_production_default_comes_from_settings(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "approval_reachable_timeout_seconds", 999.0)
    interaction = _interaction(default_timeout=300.0, reachable=lambda: True)

    assert interaction._timeout_for(_card()) == 999.0


@pytest.mark.asyncio
async def test_an_unanswered_card_is_distinguishable_from_a_refusal() -> None:
    """ "Nobody was there" and "the user said no" must not collapse into one outcome.

    An agent told it was refused tries another route, hits another card, and
    stalls again on a loop that never ends.
    """
    interaction = _interaction(default_timeout=0.01, reachable=lambda: False)

    resolved = await interaction.request_decision(_card())

    assert resolved.status == ApprovalDecision.TIMEOUT_REJECTED
    assert resolved.status != ApprovalDecision.REJECTED


@pytest.mark.asyncio
async def test_an_explicit_timeout_still_wins() -> None:
    """A caller that named a timeout meant it, whatever north can reach."""
    interaction = _interaction(default_timeout=300.0, reachable_timeout=43_200.0, reachable=lambda: True)

    resolved = await interaction.request_decision(_card(), timeout=0.01)

    assert resolved.status == ApprovalDecision.TIMEOUT_REJECTED
