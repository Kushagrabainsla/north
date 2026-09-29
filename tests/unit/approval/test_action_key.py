"""A card carries the identity of the action it asks about, so an answer is learned
under the key the policy recalls it by (regression from 64789a2: it never was)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from approval.approval_memory import ApprovalMemory
from approval.approvals import Request
from approval.interaction import UserInteraction
from approval.models import Card, CardType
from approval.policy import Action, ActionKind, ApprovalPolicy
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from tests.conftest import approvals


@pytest.mark.asyncio
async def test_a_tool_card_carries_its_actions_identity(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    action = Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary="make", command="make")

    waiting = asyncio.create_task(approvals(store=store).decide(Request(action, "t", "```\nmake\n```"), task_id=None))
    while not store.pending():
        await asyncio.sleep(0.01)
    waiting.cancel()

    assert store.all()[0].action_key == action.describe()


@pytest.mark.asyncio
async def test_a_direct_card_is_stamped_with_the_key_the_policy_recalls(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    memory = ApprovalMemory(tmp_path / "m.db")
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.SAFE, approval_memory=memory)
    interaction = UserInteraction(store, policy=policy)

    def card() -> Card:
        return Card.new(type=CardType.APPROVAL, agent="general", title="t", message="Archive old notes?")

    waiting = asyncio.create_task(interaction.request_decision(card()))
    while not store.pending():
        await asyncio.sleep(0.01)
    raised = store.pending()[0]
    memory.record(raised.agent, raised.action_key, "approved")
    store.resolve(raised.id, "approved", chosen_option="Approve")
    await waiting

    again = await asyncio.wait_for(interaction.request_decision(card()), 1)  # replayed, so it never waits

    assert again.status == "approved"
