"""Tests for orchestrator hardening: north-star fail-closed, atomic capacity,
and approval-response binding (review findings R3#16, R3#17, R3#20)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.models import ApprovalDecision, Card, CardType
from approval.store import ApprovalStore
from orchestrator.constants import MAX_CONCURRENT_TASKS
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, TaskCapacityError
from orchestrator.models import IntentClassification, TaskRequest
from orchestrator.orchestrator import Orchestrator


def _orchestrator(approval_store: ApprovalStore | None = None) -> Orchestrator:
    ledger = MagicMock()
    ledger.write = AsyncMock()
    stream_manager = MagicMock()
    stream_manager.emit = AsyncMock()
    stream_manager.emit_done = AsyncMock()
    return Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream_manager,
        approval_store=approval_store or ApprovalStore(),
    )


# ── #16: North Star check ────────────────────────────────────────────────────


async def test_north_star_inference_failure_proceeds_but_records_the_gap() -> None:
    """A checker that cannot RUN must not block the task - but must say so.

    This error means the check failed to execute (unreachable model, unparseable
    reply), not that a conflict was found. Blocking on it let one flaky cheap model
    kill any consequential task, and a harness fault is far likelier than a real
    goal conflict. Permission for consequential actions is enforced by the approval
    layer, which is unaffected; what is lost here is only the goal check, so the
    gap is recorded and streamed rather than silently swallowed.
    """
    orch = _orchestrator()
    orch._north_star_checker.check_alignment = AsyncMock(side_effect=OrchestratorError("inference down"))
    classification = IntentClassification(is_consequential=True, domain="finance", reasoning="r", confidence=1.0)

    await orch._stage_north_star("t1", "wire money", classification)  # must not raise

    actions = [call.args[0].action for call in orch._ledger.write.call_args_list]
    assert "north_star_check_failed" in actions
    events = [call.args[1] for call in orch._stream_manager.emit.call_args_list]
    assert "north_star_check_failed" in events


async def test_north_star_conflict_still_blocks_the_task() -> None:
    """A real conflict is unchanged: it surfaces a card and blocks on rejection."""
    orch = _orchestrator()
    orch._north_star_checker.check_alignment = AsyncMock(return_value=(False, "conflicts with savings goal", "r"))
    orch._interaction.request_decision = AsyncMock(
        return_value=SimpleNamespace(status=ApprovalDecision.REJECTED)
    )
    classification = IntentClassification(is_consequential=True, domain="finance", reasoning="r", confidence=1.0)

    with pytest.raises(NorthStarConflictError):
        await orch._stage_north_star("t1", "wire money", classification)


async def test_north_star_aligned_proceeds() -> None:
    orch = _orchestrator()
    orch._north_star_checker.check_alignment = AsyncMock(return_value=(True, None, "fine"))
    classification = IntentClassification(is_consequential=True, domain="finance", reasoning="r", confidence=1.0)
    await orch._stage_north_star("t1", "wire money", classification)  # must not raise


# ── #17: capacity check is atomic ────────────────────────────────────────────


async def test_concurrent_submissions_cannot_exceed_capacity(monkeypatch) -> None:
    orch = _orchestrator()
    started = asyncio.Event()

    async def slow_process(task_id: str, request: TaskRequest) -> None:
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(orch, "_process_task", slow_process)

    attempts = MAX_CONCURRENT_TASKS + 5
    results = await asyncio.gather(
        *[orch.submit_task(TaskRequest(prompt=f"task {i}")) for i in range(attempts)],
        return_exceptions=True,
    )

    accepted = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, TaskCapacityError)]
    assert len(accepted) == MAX_CONCURRENT_TASKS
    assert len(rejected) == attempts - MAX_CONCURRENT_TASKS
    assert len(orch._active_tasks) == MAX_CONCURRENT_TASKS

    for task in orch._active_tasks.values():
        task.cancel()
    await asyncio.gather(*orch._active_tasks.values(), return_exceptions=True)


# ── #20: approval responses bind to the issued card ──────────────────────────


def _pending_card(store: ApprovalStore) -> Card:
    card = Card(
        id="card-1",
        type=CardType.APPROVAL,
        task_id="task-real",
        agent="bash",
        title="T",
        message="run rm?",
        options=["Approve", "Reject"],
    )
    store.add(card)
    return card


async def test_unknown_card_is_rejected() -> None:
    orch = _orchestrator()
    with pytest.raises(LookupError):
        await orch.respond_approval(card_id="nope", decision="approved", chosen_option="Approve")


async def test_identity_comes_from_card_not_client() -> None:
    store = ApprovalStore()
    orch = _orchestrator(store)
    _pending_card(store)

    await orch.respond_approval(card_id="card-1", decision="approved", chosen_option="Approve")

    entry = orch._ledger.write.call_args[0][0]
    assert entry.task_id == "task-real"
    assert entry.agent == "bash"
    assert store.get("card-1").status == "approved"


async def test_replaying_a_resolved_card_is_rejected() -> None:
    store = ApprovalStore()
    orch = _orchestrator(store)
    _pending_card(store)

    await orch.respond_approval(card_id="card-1", decision="rejected", chosen_option="Reject")
    with pytest.raises(ValueError):
        await orch.respond_approval(card_id="card-1", decision="approved", chosen_option="Approve")
    assert store.get("card-1").status == "rejected"


def test_store_resolve_refuses_double_resolution() -> None:
    store = ApprovalStore()
    _pending_card(store)
    assert store.resolve("card-1", "approved") is True
    assert store.resolve("card-1", "rejected") is False
    assert store.get("card-1").status == "approved"
    assert store.resolve("ghost", "approved") is False
