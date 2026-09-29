"""CODING_STYLE §13.5: a card waits until it is answered, and north reminds instead of expiring it."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardType
from approval.store import ApprovalStore
from config.strategy import NorthSettings
from flows.registry import FlowRegistry
from jobs.scheduler import SYSTEM_CRON_ENTRIES
from orchestrator.constants import MAX_CONCURRENT_TASKS
from orchestrator.orchestrator import Orchestrator
from utils.runtime_resources import resource_path


def _card(task_id: str | None = "t1", *, blocking: bool = True, age: timedelta = timedelta()) -> Card:
    card = Card.new(type=CardType.APPROVAL, task_id=task_id, agent="bash", title="Run make?", message="make")
    return card.model_copy(update={"blocking": blocking, "created_at": datetime.now(UTC) - age})


@pytest.mark.asyncio
async def test_a_card_waits_until_it_is_answered() -> None:
    store = ApprovalStore()
    interaction = UserInteraction(store)
    card = _card()

    waiting = asyncio.create_task(interaction.request_decision(card))
    await asyncio.sleep(0.05)  # far past nothing: there is no clock to run out
    assert not waiting.done()

    store.resolve(card.id, ApprovalDecision.APPROVED)

    assert (await waiting).status == ApprovalDecision.APPROVED


def test_only_blocking_pending_cards_hold_their_task() -> None:
    store = ApprovalStore()
    store.add(_card("waiting"))
    store.add(_card("prepared-work", blocking=False))
    answered = _card("answered")
    store.add(answered)
    store.resolve(answered.id, ApprovalDecision.APPROVED)

    assert store.tasks_waiting_on_you() == {"waiting"}


def test_a_task_waiting_on_you_holds_no_concurrency_slot() -> None:
    store = ApprovalStore()
    orchestrator = Orchestrator.__new__(Orchestrator)  # exercise one method, not the whole graph
    orchestrator._approval_store = store
    orchestrator._active_tasks = {f"t{i}": object() for i in range(MAX_CONCURRENT_TASKS)}
    store.add(_card("t0"))

    assert orchestrator._working_tasks() == MAX_CONCURRENT_TASKS - 1
    assert orchestrator.is_waiting_on_you("t0") and not orchestrator.is_waiting_on_you("t1")


@pytest.mark.asyncio
async def test_the_reminder_lists_what_waits_oldest_first() -> None:
    store = ApprovalStore()
    notifier = AsyncMock()
    store.add(_card("newer", age=timedelta(hours=3)))
    store.add(_card("older", age=timedelta(days=3)).model_copy(update={"title": "Submit application?"}))

    summary = await UserInteraction(store, notifier=notifier).remind_waiting()

    sent = notifier.notify.await_args.args[0]
    assert sent.type is CardType.INFORMATION
    assert sent.title == "2 waiting for you"
    assert sent.message.splitlines()[0].startswith("- Submit application? (bash), waiting 3 days")
    assert "2 waiting card" in summary


@pytest.mark.asyncio
async def test_the_reminder_stays_quiet_when_nothing_waits() -> None:
    notifier = AsyncMock()

    await UserInteraction(ApprovalStore(), notifier=notifier).remind_waiting()

    notifier.notify.assert_not_awaited()


def test_an_old_settings_file_with_a_timeout_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"autonomy": "auto", "approval_timeout_seconds": 1800.0}))

    settings = NorthSettings(path)
    settings.set_timezone("UTC")  # any save

    assert settings.autonomy.value == "safe"  # "auto" still loads, as its new name
    assert "approval_timeout_seconds" not in json.loads(path.read_text())


def test_the_reminder_ships_as_a_built_in_flow_on_a_daily_schedule() -> None:
    flow = FlowRegistry(resource_path("builtin-flows")).get("waiting-cards-reminder")
    schedule = next(entry for entry in SYSTEM_CRON_ENTRIES if entry.flow == "waiting-cards-reminder")

    assert [step.action for step in flow.steps] == ["remind_waiting_cards"]
    assert (schedule.hour, schedule.minute) == (9, 0)
