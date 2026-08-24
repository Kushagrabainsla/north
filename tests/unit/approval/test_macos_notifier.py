"""Tests for MacOSNotifier and alerter interaction."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from approval.macos import MacOSNotifier, map_alerter_action_to_decision
from approval.models import ApprovalDecision, Card, CardType
from approval.store import ApprovalStore


def _card(**overrides: object) -> Card:
    base: dict[str, object] = {
        "id": "card-mac-1",
        "type": CardType.APPROVAL,
        "task_id": "task-1",
        "agent": "coder",
        "title": "Patch file",
        "message": "Apply diff to file.py",
        "options": [],
    }
    base.update(overrides)
    return Card(**base)  # type: ignore[arg-type]


def test_map_alerter_action_approval():
    assert map_alerter_action_to_decision("Approve", CardType.APPROVAL) == (ApprovalDecision.APPROVED.value, "Approve")
    assert map_alerter_action_to_decision("Reject", CardType.APPROVAL) == (ApprovalDecision.REJECTED.value, "Reject")
    assert map_alerter_action_to_decision("Cancel", CardType.APPROVAL) == (ApprovalDecision.REJECTED.value, "Cancel")


def test_map_alerter_action_question():
    assert map_alerter_action_to_decision("Option A", CardType.QUESTION) == (ApprovalDecision.ANSWERED.value, "Option A")


@pytest.mark.asyncio
async def test_macos_notifier_resolves_approval_store():
    store = ApprovalStore()
    card = _card()
    store.add(card)

    notifier = MacOSNotifier(store=store)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"Approve\n", b"")

    await notifier._wait_and_handle_exit(mock_proc, card)

    resolved = store.get(card.id)
    assert resolved is not None
    assert resolved.status == ApprovalDecision.APPROVED.value
    assert resolved.chosen_option == "Approve"


@pytest.mark.asyncio
async def test_macos_notifier_resolves_question_store():
    store = ApprovalStore()
    card = _card(type=CardType.QUESTION, options=["Redo", "Continue"])
    store.add(card)

    notifier = MacOSNotifier(store=store)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"Continue\n", b"")

    await notifier._wait_and_handle_exit(mock_proc, card)

    resolved = store.get(card.id)
    assert resolved is not None
    assert resolved.status == ApprovalDecision.ANSWERED.value
    assert resolved.chosen_option == "Continue"


@pytest.mark.asyncio
async def test_macos_notifier_falls_back_when_no_alerter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    card = _card()
    notifier = MacOSNotifier()
    with patch.object(notifier._terminal_fallback, "notify", new_callable=AsyncMock) as mock_terminal:
        await notifier.notify(card)
        mock_terminal.assert_awaited_once_with(card)
