"""Information reaches the user without becoming approval work."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from approval.interaction import UserInteraction
from approval.models import Card, CardType
from approval.store import ApprovalStore


def _notification(**updates) -> Card:
    values = {
        "type": CardType.INFORMATION,
        "task_id": "task-1",
        "agent": "research",
        "title": "Research - Done",
        "message": "The report is ready.",
    }
    values.update(updates)
    return Card.new(**values)


def test_information_cards_are_non_blocking_by_construction() -> None:
    assert _notification().blocking is False


@pytest.mark.asyncio
async def test_inform_delivers_without_creating_approval_work() -> None:
    store = ApprovalStore()
    notifier = AsyncMock()
    interaction = UserInteraction(store, notifier=notifier)

    await interaction.inform(
        task_id="task-1",
        agent="research",
        title="Research - Done",
        message="The report is ready.",
    )

    notifier.notify.assert_awaited_once()
    delivered = notifier.notify.await_args.args[0]
    assert delivered.type is CardType.INFORMATION
    assert delivered.blocking is False
    assert store.all() == []
    assert store.pending() == []


@pytest.mark.asyncio
async def test_notification_is_scrubbed_before_external_delivery() -> None:
    store = ApprovalStore()
    notifier = AsyncMock()
    interaction = UserInteraction(store, notifier=notifier)

    delivered = await interaction.notify(_notification(message="api_key: secret-token"))

    assert "secret-token" not in delivered.message
    assert "secret-token" not in notifier.notify.await_args.args[0].message


def test_approval_store_rejects_information_cards() -> None:
    with pytest.raises(ValueError, match="do not belong"):
        ApprovalStore().add(_notification())
