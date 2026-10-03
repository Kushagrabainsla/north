"""Model-scarcity resilience: north runs with whatever model access it has.

When model access is exhausted, north labels it honestly, queues the entire task,
and surfaces exhausted recovery instead of silently accepting partial work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.interaction import UserInteraction
from approval.store import ApprovalStore
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.model_scarcity import AgentFailure, has_model_scarcity, is_model_scarcity
from orchestrator.orchestrator import Orchestrator

_PREAMBLE = "implement it"


def _orch():
    registry = MagicMock()
    registry.names.return_value = ["coder", "reviewer"]
    registry.get.side_effect = lambda n: type("A", (), {"name": n})()
    stream = MagicMock()
    stream.emit = AsyncMock()
    stream.emit_done = AsyncMock()
    ledger = MagicMock()
    ledger.write = AsyncMock()
    approval_store_ = ApprovalStore()
    return Orchestrator(
        ledger=ledger,
        agent_registry=registry,
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
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


def test_agent_failure_is_a_str_carrying_error_type():
    f = AgentFailure("reviewer", "model_unavailable")
    assert isinstance(f, str)
    assert f == "reviewer"
    assert f.error_type == "model_unavailable"
    # Flows unchanged through the existing name-based consumers.
    both = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "model_unavailable")]
    assert ", ".join(both) == "coder, reviewer"
    assert len(both) == 2


def test_is_model_scarcity_requires_every_failure_to_be_model_unavailable():
    assert is_model_scarcity([AgentFailure("coder", "model_unavailable")]) is True
    # A real bug mixed in must NOT be treated as a graceful skip.
    mixed = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "logic_error")]
    assert is_model_scarcity(mixed) is False
    # Plain strings (no error_type) count as non-model, and empty is not scarcity.
    assert is_model_scarcity(["coder"]) is False
    assert is_model_scarcity([]) is False


def test_has_model_scarcity_needs_only_one_blocked_agent():
    """The "any" twin of is_model_scarcity: one agent waiting on a model queues the task (#42)."""
    mixed = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "logic_error")]
    assert has_model_scarcity(mixed) is True
    assert has_model_scarcity([AgentFailure("reviewer", "logic_error")]) is False
    assert has_model_scarcity([]) is False


# --------------------------------------------------------------- _finish_task


@pytest.mark.asyncio
async def test_finish_task_scarcity_without_a_durable_queue_needs_attention():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failures=[AgentFailure("coder", "model_unavailable")], total_agents=1)
    assert writes[-1].action == "task_needs_attention"
    assert writes[-1].error_type == "model_unavailable"
    assert "task_needs_attention" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_finish_task_scarcity_blocks_completion_even_when_not_all_agents_failed():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failures=[AgentFailure("architect", "model_unavailable")], total_agents=4)
    assert writes[-1].action == "task_needs_attention"


@pytest.mark.asyncio
async def test_finish_task_mixed_failure_still_surfaces_model_recovery_need():
    orch = _orch()
    writes = _record_writes(orch)
    fails = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "logic_error")]
    await orch._finish_task("t1", failures=fails, total_agents=2)
    assert writes[-1].action == "task_needs_attention"


@pytest.mark.asyncio
async def test_finish_task_clean_success_unaffected():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failures=[], total_agents=1)
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
async def test_execute_agent_group_tags_real_exhaustion_as_model_unavailable():
    """The integration link the scripted tests assume: a real
    AllModelsRateLimitedError raised during agent execution must come back out of
    the real _execute_agent_group as an AgentFailure tagged model_unavailable."""
    from inference.exceptions import AllModelsRateLimitedError

    orch = _orch()
    _record_writes(orch)
    orch._heartbeat = AsyncMock()
    orch._exclude_models_for = AsyncMock(return_value=[])

    async def boom(agent, payload):
        raise AllModelsRateLimitedError("No completion models are available")

    orch._run_agent_with_retry = boom
    agent = type("A", (), {"name": "reviewer"})()
    failures = await orch._execute_agent_group("t1", "review it", [agent])
    assert failures == ["reviewer"]
    assert failures[0].error_type == "model_unavailable"
    assert is_model_scarcity(failures) is True
