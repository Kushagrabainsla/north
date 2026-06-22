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
    return JudgementFilter(context_store=context_store, inference_router=router)


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
