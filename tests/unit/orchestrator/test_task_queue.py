"""Unit tests for task queueing and model-recovery auto-drain."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.store import ApprovalStore
from inference.exceptions import AllModelsRateLimitedError
from ledger import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.constants import MAX_QUEUE_ATTEMPTS
from orchestrator.models import TaskRequest
from orchestrator.orchestrator import Orchestrator
from orchestrator.running_tasks import RunningTaskStore


@pytest.fixture
def running_task_store(tmp_path) -> RunningTaskStore:
    return RunningTaskStore(tmp_path / "running_tasks.db")


def _orch(running_task_store: RunningTaskStore | None = None):
    registry = MagicMock()
    registry.names.return_value = ["coder", "reviewer"]
    registry.get.side_effect = lambda n: type("A", (), {"name": n})()
    stream = MagicMock()
    stream.emit = AsyncMock()
    stream.emit_done = AsyncMock()
    ledger = MagicMock()
    ledger.write = AsyncMock()
    ledger.query = AsyncMock(return_value=[])
    ledger.query_summaries = AsyncMock(return_value=[])
    orch = Orchestrator(
        ledger=ledger,
        agent_registry=registry,
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream,
        approval_store=ApprovalStore(),
        running_task_store=running_task_store,
    )
    orch._task_context_store.release_conditions = MagicMock()
    return orch


def _record_writes(orch):
    writes: list[LedgerEntry] = []

    async def rec(entry):
        writes.append(entry)

    orch._write_ledger = rec
    return writes


def _emitted_events(orch):
    return [c.args[1] for c in orch._stream_manager.emit.call_args_list]


# ------------------------------------------------ RunningTaskStore Queue Tests


async def test_running_task_store_queue_lifecycle(running_task_store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="test queue", source=LedgerSource.PROMPT)
    await running_task_store.mark_running("t1", req, attempt=0)

    # Initial state
    all_tasks = await running_task_store.list_all()
    assert len(all_tasks) == 1
    assert all_tasks[0].status == "running"

    # Transition to queued
    queued_ok = await running_task_store.mark_queued("t1", attempt=1)
    assert queued_ok is True

    queued = await running_task_store.list_queued()
    assert len(queued) == 1
    assert queued[0].task_id == "t1"
    assert queued[0].attempt == 1
    assert queued[0].status == "queued"

    # Transition from queued to running
    resumed_ok = await running_task_store.mark_running_from_queued("t1")
    assert resumed_ok is True

    queued_after = await running_task_store.list_queued()
    assert len(queued_after) == 0

    running_after = await running_task_store.list_all()
    assert len(running_after) == 1
    assert running_after[0].status == "running"


# ------------------------------------------------ Orchestrator Queueing on Model Scarcity


@pytest.mark.asyncio
async def test_model_scarcity_queues_task(running_task_store: RunningTaskStore) -> None:
    orch = _orch(running_task_store)
    writes = _record_writes(orch)

    req = TaskRequest(prompt="write code", source=LedgerSource.PROMPT)
    await running_task_store.mark_running("t1", req, attempt=0)

    # Mock _stage_plan to raise AllModelsRateLimitedError
    orch._stage_plan = AsyncMock(side_effect=AllModelsRateLimitedError("No models available"))

    await orch._process_task("t1", req)

    # Verify task was queued
    queued = await running_task_store.list_queued()
    assert len(queued) == 1
    assert queued[0].task_id == "t1"
    assert queued[0].attempt == 1

    # Verify ledger and events
    assert writes[-1].action == "task_queued"
    assert writes[-1].status == LedgerStatus.PENDING
    assert "task_queued" in _emitted_events(orch)
    assert orch._queue_wake_event.is_set()


@pytest.mark.asyncio
async def test_task_exceeds_max_queue_attempts_is_skipped(running_task_store: RunningTaskStore) -> None:
    orch = _orch(running_task_store)
    writes = _record_writes(orch)

    req = TaskRequest(prompt="write code", source=LedgerSource.PROMPT)
    # Attempt count set to max
    await running_task_store.mark_running("t1", req, attempt=MAX_QUEUE_ATTEMPTS)

    orch._stage_plan = AsyncMock(side_effect=AllModelsRateLimitedError("No models available"))

    await orch._process_task("t1", req)

    # Should be cleared from running_task_store and marked skipped
    queued = await running_task_store.list_queued()
    assert len(queued) == 0

    assert writes[-1].action == "task_skipped_model_unavailable"
    assert "task_skipped" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_cancel_queued_task(running_task_store: RunningTaskStore) -> None:
    orch = _orch(running_task_store)
    writes = _record_writes(orch)

    req = TaskRequest(prompt="write code", source=LedgerSource.PROMPT)
    await running_task_store.mark_running("t1", req)
    await running_task_store.mark_queued("t1", attempt=1)

    cancelled = await orch.cancel_task("t1")
    assert cancelled is True

    # Should be cleared
    queued = await running_task_store.list_queued()
    assert len(queued) == 0

    assert writes[-1].action == "task_cancelled"
    assert "task_cancelled" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_get_task_reports_queued_status(running_task_store: RunningTaskStore) -> None:
    orch = _orch(running_task_store)
    req = TaskRequest(prompt="write code", source=LedgerSource.PROMPT)
    await running_task_store.mark_running("t1", req)
    await running_task_store.mark_queued("t1", attempt=1)

    entry = LedgerEntry.new(
        source=LedgerSource.SYSTEM,
        task_id="t1",
        action="task_queued",
        status=LedgerStatus.PENDING,
    )
    orch._ledger.query_summaries = AsyncMock(return_value=[entry])

    resp = await orch.get_task("t1")
    assert resp is not None
    assert resp.status == "queued"


@pytest.mark.asyncio
async def test_drain_queued_tasks_loop_resumes_task(running_task_store: RunningTaskStore) -> None:
    orch = _orch(running_task_store)
    writes = _record_writes(orch)

    req = TaskRequest(prompt="write code", source=LedgerSource.PROMPT)
    await running_task_store.mark_running("t1", req)
    await running_task_store.mark_queued("t1", attempt=1)

    processed_tasks = []

    async def mock_process(task_id, req):
        processed_tasks.append(task_id)

    orch._process_task = mock_process

    # Run one tick of the drainer loop
    drain_task = asyncio.create_task(orch.drain_queued_tasks_loop(poll_interval=0.01))
    orch.notify_model_recovery()

    await asyncio.sleep(0.05)
    drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drain_task

    assert "t1" in processed_tasks
    queued = await running_task_store.list_queued()
    assert len(queued) == 0
    assert writes[-1].action == "task_resumed"

