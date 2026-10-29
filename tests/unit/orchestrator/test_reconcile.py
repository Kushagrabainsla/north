"""Unit tests for startup recovery (orchestrator/reconcile.py).

Drives recover_interrupted_tasks with a stub RunningTaskStore/ledger/orchestrator
so the resume/fail decision logic is exercised in isolation: interrupted tasks
resume (attempt incremented), the poison-pill cap and max-age fail, and
active tasks are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ledger.models import LedgerSource
from orchestrator.constants import MAX_RESUME_ATTEMPTS
from orchestrator.models import TaskRequest
from orchestrator.reconcile import recover_interrupted_tasks
from orchestrator.running_tasks import RunningTask

_MAX_AGE = 86_400


def _running_task(
    task_id: str,
    *,
    attempt: int = 0,
    age_seconds: float = 1.0,
    heartbeat_age: float | None = None,
    has_side_effects: bool = False,
    status: str = "running",
) -> RunningTask:
    now = datetime.now(UTC)
    hb_age = age_seconds if heartbeat_age is None else heartbeat_age
    return RunningTask(
        task_id=task_id,
        request=TaskRequest(prompt=f"prompt {task_id}", source=LedgerSource.PROMPT, workspace="/ws"),
        attempt=attempt,
        started_at=now - timedelta(seconds=age_seconds),
        heartbeat_at=now - timedelta(seconds=hb_age),
        has_side_effects=has_side_effects,
        status=status,
    )


class _StubStore:
    def __init__(self, tasks: list[RunningTask]) -> None:
        self._tasks = tasks
        self.cleared: list[str] = []
        self.queued: list[tuple[str, int]] = []

    async def list_all(self) -> list[RunningTask]:
        return list(self._tasks)

    async def clear(self, task_id: str) -> None:
        self.cleared.append(task_id)

    async def mark_queued(self, task_id: str, *, attempt: int = 0) -> bool:
        self.queued.append((task_id, attempt))
        return True


class _StubLedger:
    def __init__(self) -> None:
        self.writes: list[object] = []

    async def write(self, entry: object) -> None:
        self.writes.append(entry)


class _StubDeps:
    def __init__(self, ledger: _StubLedger, store: _StubStore) -> None:
        self.ledger = ledger
        self.running_task_store = store


class _StubOrchestrator:
    def __init__(self, active: tuple[str, ...] = (), resume_result: bool = True) -> None:
        self.active_task_ids = frozenset(active)
        self._resume_result = resume_result
        self.resumed: list[tuple[str, TaskRequest, int]] = []

    async def resume_task(self, task_id: str, request: TaskRequest, *, attempt: int) -> bool:
        self.resumed.append((task_id, request, attempt))
        return self._resume_result


def _failed_ids(ledger: _StubLedger) -> list[str]:
    return [e.task_id for e in ledger.writes if getattr(e, "action", None) == "task_failed"]


async def _run(deps: _StubDeps, orch: _StubOrchestrator) -> None:
    await recover_interrupted_tasks(deps, orch, max_age_seconds=_MAX_AGE)


async def test_resumes_interrupted_task_with_incremented_attempt() -> None:
    store = _StubStore([_running_task("t1", attempt=1)])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert len(orch.resumed) == 1
    task_id, request, attempt = orch.resumed[0]
    assert task_id == "t1"
    assert attempt == 2  # incremented from 1
    assert request.workspace == "/ws"  # full request reconstructed
    assert _failed_ids(ledger) == []
    assert store.cleared == []


async def test_poison_pill_fails_at_attempt_cap() -> None:
    store = _StubStore([_running_task("t1", attempt=MAX_RESUME_ATTEMPTS)])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert _failed_ids(ledger) == ["t1"]
    assert store.cleared == ["t1"]


async def test_fails_task_older_than_max_age() -> None:
    store = _StubStore([_running_task("t1", attempt=0, age_seconds=_MAX_AGE + 3600)])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert _failed_ids(ledger) == ["t1"]
    assert store.cleared == ["t1"]


async def test_skips_active_task() -> None:
    store = _StubStore([_running_task("t1")])
    ledger, orch = _StubLedger(), _StubOrchestrator(active=("t1",))

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert _failed_ids(ledger) == []
    assert store.cleared == []


async def test_no_interrupted_tasks_is_noop() -> None:
    store = _StubStore([])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert ledger.writes == []


async def test_mixed_batch_resumes_fails_and_skips() -> None:
    store = _StubStore(
        [
            _running_task("resume-me", attempt=0),
            _running_task("too-old", age_seconds=_MAX_AGE + 1),
            _running_task("poison", attempt=MAX_RESUME_ATTEMPTS),
            _running_task("active", attempt=0),
        ]
    )
    ledger, orch = _StubLedger(), _StubOrchestrator(active=("active",))

    await _run(_StubDeps(ledger, store), orch)

    assert [tid for tid, _, _ in orch.resumed] == ["resume-me"]
    assert sorted(_failed_ids(ledger)) == ["poison", "too-old"]
    assert sorted(store.cleared) == ["poison", "too-old"]


@pytest.mark.parametrize("resume_ok", [True, False])
async def test_resume_result_does_not_crash(resume_ok: bool) -> None:
    store = _StubStore([_running_task("t1")])
    ledger, orch = _StubLedger(), _StubOrchestrator(resume_result=resume_ok)
    await _run(_StubDeps(ledger, store), orch)
    assert [tid for tid, _, _ in orch.resumed] == ["t1"]


async def test_side_effecting_task_not_resumed_by_default() -> None:
    store = _StubStore([_running_task("t1", has_side_effects=True)])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await recover_interrupted_tasks(_StubDeps(ledger, store), orch, max_age_seconds=_MAX_AGE)

    assert orch.resumed == []  # not blindly re-run
    assert _failed_ids(ledger) == ["t1"]
    assert store.cleared == ["t1"]


async def test_side_effecting_task_resumed_when_opted_in() -> None:
    store = _StubStore([_running_task("t1", has_side_effects=True)])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await recover_interrupted_tasks(
        _StubDeps(ledger, store), orch, max_age_seconds=_MAX_AGE, resume_side_effecting=True
    )

    assert [tid for tid, _, _ in orch.resumed] == ["t1"]
    assert _failed_ids(ledger) == []


async def test_a_task_queued_for_a_model_is_left_to_the_queue_however_long_it_waited() -> None:
    """Three outages, or a day of one, failed it at the next start; a missing model never uses up attempts (#34)."""
    store = _StubStore(
        [_running_task("t1", attempt=MAX_RESUME_ATTEMPTS + 2, age_seconds=_MAX_AGE * 2, status="queued")]
    )
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert _failed_ids(ledger) == []
    assert store.cleared == []


async def test_a_task_the_queue_was_running_again_goes_back_to_the_queue() -> None:
    store = _StubStore([_running_task("t1", attempt=MAX_RESUME_ATTEMPTS + 2, status="retrying")])
    ledger, orch = _StubLedger(), _StubOrchestrator()

    await _run(_StubDeps(ledger, store), orch)

    assert orch.resumed == []
    assert _failed_ids(ledger) == []
    assert store.queued == [("t1", MAX_RESUME_ATTEMPTS + 2)]


async def test_a_task_north_has_no_room_for_yet_waits_in_the_queue() -> None:
    """Refused at capacity, its row read "running" with nothing running it until the next start."""
    store = _StubStore([_running_task("t1", attempt=1)])
    ledger, orch = _StubLedger(), _StubOrchestrator(resume_result=False)

    await _run(_StubDeps(ledger, store), orch)

    assert store.queued == [("t1", 1)]
    assert _failed_ids(ledger) == []
