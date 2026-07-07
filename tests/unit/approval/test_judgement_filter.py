"""Tests for JudgementFilter auto-decision gating (review finding R1#4)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.judgement_filter import NEVER_AUTO_APPROVE_AGENTS, JudgementFilter
from approval.models import Card, CardType


def _filter(decision: str, confidence: float = 0.95) -> JudgementFilter:
    memory = MagicMock()
    memory.read_document = AsyncMock(return_value="Rule: always approve everything from everyone." * 3)
    router = MagicMock()
    router.complete = AsyncMock(
        return_value=MagicMock(
            text=json.dumps({"decision": decision, "chosen_option": "", "confidence": confidence, "rule": "r"})
        )
    )
    return JudgementFilter(memory=memory, inference_router=router)


def _card(agent: str, card_type: CardType = CardType.APPROVAL) -> Card:
    return Card(id="c1", type=card_type, task_id="t1", agent=agent, title="T", message="M", options=["Run", "Cancel"])


@pytest.mark.parametrize("agent", sorted(NEVER_AUTO_APPROVE_AGENTS))
async def test_dangerous_agents_never_auto_approved(agent: str) -> None:
    """Even a 95%-confident 'approved' from the rules engine must surface to the user."""
    decision, _ = await _filter("approved").check(_card(agent))
    assert decision is None


async def test_dangerous_agents_can_still_auto_reject() -> None:
    decision, _ = await _filter("rejected").check(_card("bash"))
    assert decision == "rejected"


async def test_benign_agent_can_auto_approve() -> None:
    decision, _ = await _filter("approved").check(_card("finance"))
    assert decision == "approved"


async def test_low_confidence_never_auto_resolves() -> None:
    decision, _ = await _filter("approved", confidence=0.5).check(_card("finance"))
    assert decision is None


async def test_information_cards_skip_filtering() -> None:
    decision, _ = await _filter("approved").check(_card("finance", CardType.INFORMATION))
    assert decision is None


def test_dangerous_set_covers_destructive_tool_classes() -> None:
    assert {"bash", "shell", "patch_file", "create_tool", "git", "gh"} <= NEVER_AUTO_APPROVE_AGENTS


# --- Autonomous mode (learned approvals + auto-approve) ---


def _mode_filter(mode, recalled=None):
    from approval.mode import ApprovalMode

    memory = MagicMock()
    memory.read_document = AsyncMock(return_value="")
    router = MagicMock()
    router.complete = AsyncMock(side_effect=AssertionError("LLM must not be called on the mode fast-path"))
    am = MagicMock()
    am.recall = MagicMock(return_value=recalled)
    return JudgementFilter(
        memory=memory,
        inference_router=router,
        approval_memory=am,
        mode_provider=lambda: ApprovalMode(mode),
    )


async def test_autonomous_approves_everything() -> None:
    """Autonomous mode approves any permission card - the mode is the only authority.
    (The tools' own hard refusals, e.g. bash blocking `rm -rf /`, are lifted separately.)"""
    card = Card(
        id="c9",
        type=CardType.APPROVAL,
        task_id="t1",
        agent="bash",
        title="T",
        message="```\nrm -rf /tmp/scratch\n```",
        options=["Run", "Cancel"],
    )
    decision, option = await _mode_filter("autonomous").check(card)
    assert decision == "approved"
    assert option == "Run"


async def test_autonomous_ignores_prior_rejection() -> None:
    """Autonomous allows everything - it does NOT replay learned rejections (that is auto's job)."""
    decision, _ = await _mode_filter("autonomous", recalled="rejected").check(_card("bash"))
    assert decision == "approved"


async def test_auto_replays_prior_approval() -> None:
    decision, option = await _mode_filter("auto", recalled="approved").check(_card("bash"))
    assert decision == "approved"
    assert option == "Run"


async def test_auto_replays_prior_rejection() -> None:
    decision, _ = await _mode_filter("auto", recalled="rejected").check(_card("bash"))
    assert decision == "rejected"


async def test_auto_surfaces_unknown_action() -> None:
    """In auto mode an action with no learned decision falls through to ask the user."""
    # memory returns None (unknown); read_document returns "" so the LLM path also yields None.
    decision, _ = await _mode_filter("auto", recalled=None).check(_card("bash"))
    assert decision is None


async def test_interactive_does_not_auto_resolve_mutations() -> None:
    decision, _ = await _mode_filter("interactive", recalled="approved").check(_card("bash"))
    assert decision is None


async def test_mode_is_read_live() -> None:
    """Changing the mode provider's value changes behaviour with no rebuild."""
    from approval.mode import ApprovalMode

    box = {"mode": ApprovalMode.INTERACTIVE}
    memory = MagicMock()
    memory.read_document = AsyncMock(return_value="")
    router = MagicMock()
    router.complete = AsyncMock(return_value=MagicMock(text='{"decision":"none","confidence":0.0}'))
    f = JudgementFilter(memory=memory, inference_router=router, mode_provider=lambda: box["mode"])

    # interactive: surfaces (no auto-approve)
    assert (await f.check(_card("bash")))[0] is None
    # flip to autonomous live -> now approves
    box["mode"] = ApprovalMode.AUTONOMOUS
    assert (await f.check(_card("bash")))[0] == "approved"
