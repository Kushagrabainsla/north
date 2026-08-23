"""Model-scarcity resilience: north runs with whatever model access it has.

When the whole model pool is exhausted, north must (1) label it honestly as
"model unavailable" rather than a logic bug, (2) end the task as a distinct
"skipped" outcome instead of a generic failure, and (3) never let a *non-critical*
step's scarcity (the independent reviewer, whose coder already finished) sink a
task whose real work is done. None of this lowers north's rigor - it only makes
model scarcity graceful and transparent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.store import ApprovalStore
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.orchestrator import AgentFailure, Orchestrator, _is_model_scarcity

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
    assert _is_model_scarcity([AgentFailure("coder", "model_unavailable")]) is True
    # A real bug mixed in must NOT be treated as a graceful skip.
    mixed = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "logic_error")]
    assert _is_model_scarcity(mixed) is False
    # Plain strings (no error_type) count as non-model, and empty is not scarcity.
    assert _is_model_scarcity(["coder"]) is False
    assert _is_model_scarcity([]) is False


# --------------------------------------------------------------- _finish_task


@pytest.mark.asyncio
async def test_finish_task_scarcity_writes_skipped_not_failed():
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failures=[AgentFailure("coder", "model_unavailable")], total_agents=1)
    assert writes[-1].action == "task_skipped_model_unavailable"
    assert writes[-1].error_type == "model_unavailable"
    assert "task_skipped" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_finish_task_scarcity_skips_even_when_not_all_agents_failed():
    # Design flow: total_agents=4 but only the architect was blocked by scarcity
    # (downstream never ran). all_failed is False, yet the honest outcome is skip.
    orch = _orch()
    writes = _record_writes(orch)
    await orch._finish_task("t1", failures=[AgentFailure("architect", "model_unavailable")], total_agents=4)
    assert writes[-1].action == "task_skipped_model_unavailable"


@pytest.mark.asyncio
async def test_finish_task_mixed_failure_is_never_skipped():
    orch = _orch()
    writes = _record_writes(orch)
    fails = [AgentFailure("coder", "model_unavailable"), AgentFailure("reviewer", "logic_error")]
    await orch._finish_task("t1", failures=fails, total_agents=2)
    assert writes[-1].action == "task_failed"  # a real bug is a failure, not a skip


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
    orch._ledger.query = AsyncMock(return_value=[entry])
    resp = await orch.get_task("t1")
    assert resp is not None
    assert resp.status == "skipped"


# --------------------------------------------------------------- conductor (Fix 3)


@pytest.mark.asyncio
async def test_reviewer_scarcity_does_not_sink_the_task():
    orch = _orch()
    _record_writes(orch)
    # coder succeeds ([]), then the reviewer can't get any model (scarcity).
    seq = [[], [AgentFailure("reviewer", "model_unavailable")]]

    async def fake_group(task_id, prompt, agents, workspace="", context="", allow_delegation=True, *args, **kwargs):
        return seq.pop(0)

    orch._execute_agent_group = fake_group
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    # Task proceeds (coder work preserved); the DoD gate will mark the missing review.
    assert failures == []
    assert "conductor_review_skipped_model_unavailable" in _emitted_events(orch)


@pytest.mark.asyncio
async def test_genuine_reviewer_failure_still_returns():
    orch = _orch()
    _record_writes(orch)
    # coder ok, then a REAL reviewer failure (not scarcity) - unchanged behaviour.
    seq = [[], [AgentFailure("reviewer", "logic_error")]]

    async def fake_group(task_id, prompt, agents, workspace="", context="", allow_delegation=True, *args, **kwargs):
        return seq.pop(0)

    orch._execute_agent_group = fake_group
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == ["reviewer"]


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

    orch._run_agent_isolated_or_direct = boom
    agent = type("A", (), {"name": "reviewer"})()
    failures = await orch._execute_agent_group("t1", "review it", [agent])
    assert failures == ["reviewer"]
    assert failures[0].error_type == "model_unavailable"
    assert _is_model_scarcity(failures) is True
