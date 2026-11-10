"""Model-scarcity resilience: north runs with whatever model access it has.

When model access is exhausted, north labels it honestly, queues the task, and
surfaces exhausted recovery instead of silently accepting partial work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.interaction import UserInteraction
from approval.store import ApprovalStore
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.model_scarcity import AgentFailure
from orchestrator.orchestrator import Orchestrator


def _orch():
    stream = MagicMock()
    stream.emit = AsyncMock()
    stream.emit_done = AsyncMock()
    ledger = MagicMock()
    ledger.write = AsyncMock()
    approval_store_ = ApprovalStore()
    agent = MagicMock()
    agent.name = "north"
    return Orchestrator(
        ledger=ledger,
        agent=agent,
        north_star_checker=MagicMock(),
        failure_handler=MagicMock(),
        interaction=UserInteraction(approval_store_, notifier=MagicMock(), stream_manager=stream),
        stream_manager=stream,
        approval_store=approval_store_,
    )


def _record_writes(orch):
    writes: list[LedgerEntry] = []

    async def rec(entry):
        writes.append(entry)

    orch._journal.write = rec
    return writes


def _emitted_events(orch):
    return [c.args[1] for c in orch._stream_manager.emit.call_args_list]


# --------------------------------------------------------------- AgentFailure


def test_agent_failure_carries_its_reason_and_kind():
    f = AgentFailure("No completion models are available", "model_unavailable")
    assert f.reason == "No completion models are available"
    assert f.error_type == "model_unavailable"


# --------------------------------------------------------------- _finish_task


@pytest.mark.asyncio
async def test_finish_task_scarcity_without_a_durable_queue_needs_attention():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failure=AgentFailure("no model", "model_unavailable"))
    assert writes[-1].action == "task_needs_attention"
    assert writes[-1].error_type == "model_unavailable"
    assert "task_needs_attention" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_finish_task_a_real_failure_ends_the_task_failed_with_its_reason():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failure=AgentFailure("boom", "logic_error"))
    assert writes[-1].action == "task_failed"
    assert writes[-1].output == "boom"
    assert writes[-1].error_type == "logic_error"
    assert "task_failed" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_finish_task_clean_success_unaffected():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1")
    assert writes[-1].action == "task_completed"


# --------------------------------------------------------------- get_task


@pytest.mark.asyncio
async def test_get_task_reports_skipped_status():
    orch = _orch()
    entry = LedgerEntry.new(
        source=LedgerSource.SYSTEM,
        task_id="t1",
        action="task_skipped_model_unavailable",
        status=LedgerStatus.FAILED,
    )
    orch._ledger.query_summaries = AsyncMock(return_value=[entry])
    resp = await orch.get_task("t1")
    assert resp is not None
    assert resp.status == "skipped"


@pytest.mark.asyncio
async def test_run_agent_tags_real_exhaustion_as_model_unavailable():
    """A real AllModelsRateLimitedError raised while north runs comes back out of
    _run_agent as an AgentFailure tagged model_unavailable, so the task is queued."""
    from inference.exceptions import AllModelsRateLimitedError

    orch = _orch()
    _record_writes(orch)
    orch._heartbeat = AsyncMock()

    async def boom(agent, payload):
        raise AllModelsRateLimitedError("No completion models are available")

    orch._run_agent_with_retry = boom
    failure = await orch._run_agent("t1", "review it", "/tmp", "", "standard", None)
    assert failure is not None
    assert failure.error_type == "model_unavailable"


@pytest.mark.asyncio
async def test_a_task_waiting_for_a_model_streams_no_failure_note():
    """It runs again, so nothing is final: no failure text reaches the answer."""
    orch = _orch()
    _record_writes(orch)

    await orch._finish_task("t1", failure=AgentFailure("no model", "model_unavailable"))

    assert "token" not in _emitted_events(orch)
    assert "task_failed" not in _emitted_events(orch)
