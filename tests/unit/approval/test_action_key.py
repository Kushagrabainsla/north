"""A card carries the identity of the action it asks about, so an answer is learned
under the key the policy recalls it by (regression from 64789a2: it never was)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from approval.approval_memory import ApprovalMemory
from approval.interaction import UserInteraction
from approval.models import Card, CardType
from approval.policy import Action, ActionKind, ApprovalPolicy
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from tools.specialized._approval import gate_action


@pytest.mark.asyncio
async def test_a_tool_card_carries_its_actions_identity(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    action = Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary="make", command="make")

    await gate_action(action, policy=None, approval_store=store, title="t", message="```\nmake\n```", timeout=0.01)

    assert store.all()[0].action_key == action.describe()


@pytest.mark.asyncio
async def test_a_direct_card_is_stamped_with_the_key_the_policy_recalls(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "a.db")
    memory = ApprovalMemory(tmp_path / "m.db")
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.AUTO, approval_memory=memory)
    interaction = UserInteraction(store, policy=policy, reachable=lambda: False)

    def card() -> Card:
        return Card.new(type=CardType.APPROVAL, agent="general", title="t", message="Archive old notes?")

    waiting = asyncio.create_task(interaction.request_decision(card(), timeout=5))
    while not store.pending():
        await asyncio.sleep(0.01)
    raised = store.pending()[0]
    memory.record(raised.agent, raised.action_key, "approved")
    store.resolve(raised.id, "approved", chosen_option="Approve")
    await waiting

    again = await interaction.request_decision(card(), timeout=0.01)

    assert again.status == "approved"
