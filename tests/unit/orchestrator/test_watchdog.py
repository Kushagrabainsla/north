"""Unit tests for the stuck-task watchdog sweep (orchestrator/watchdog.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ledger.models import LedgerSource
from orchestrator.models import TaskRequest
from orchestrator.running_tasks import RunningTask
from orchestrator.watchdog import _sweep

_MAX_AGE = 86_400


def _task(task_id: str, *, heartbeat_age: float) -> RunningTask:
    now = datetime.now(UTC)
    return RunningTask(
        task_id=task_id,
        request=TaskRequest(prompt="p", source=LedgerSource.PROMPT),
        attempt=0,
        started_at=now - timedelta(seconds=heartbeat_age),
        heartbeat_at=now - timedelta(seconds=heartbeat_age),
    )


class _StubStore:
    def __init__(self, tasks: list[RunningTask]) -> None:
        self._tasks = tasks

    async def list_all(self) -> list[RunningTask]:
        return list(self._tasks)

    async def heartbeat(self, task_id: str) -> None:
        self._tasks = [
            replace(task, heartbeat_at=datetime.now(UTC)) if task.task_id == task_id else task for task in self._tasks
        ]


class _StubOrchestrator:
    def __init__(self, tasks: list[RunningTask], *, waiting_on_you: frozenset[str] = frozenset()) -> None:
        self._store = _StubStore(tasks)
        self._waiting_on_you = waiting_on_you
        self.cancelled: list[str] = []

    def is_waiting_on_you(self, task_id: str) -> bool:
        return task_id in self._waiting_on_you

    @property
    def running_task_store(self) -> _StubStore:
        return self._store

    async def cancel_stuck_task(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


async def test_a_task_waiting_on_you_is_not_stuck_however_long_it_waits() -> None:
    """Cards never expire (CODING_STYLE §13.5), so waiting is not stalling."""
    orch = _StubOrchestrator([_task("t1", heartbeat_age=_MAX_AGE * 10)], waiting_on_you=frozenset({"t1"}))
    await _sweep(orch, _MAX_AGE)
    assert orch.cancelled == []


async def test_fresh_task_is_not_cancelled() -> None:
    orch = _StubOrchestrator([_task("t1", heartbeat_age=10)])
    await _sweep(orch, _MAX_AGE)
    assert orch.cancelled == []


async def test_stalled_task_is_cancelled() -> None:
    orch = _StubOrchestrator([_task("t1", heartbeat_age=_MAX_AGE + 60)])
    await _sweep(orch, _MAX_AGE)
    assert orch.cancelled == ["t1"]


async def test_only_stalled_tasks_cancelled() -> None:
    orch = _StubOrchestrator(
        [
            _task("fresh", heartbeat_age=5),
            _task("stalled", heartbeat_age=_MAX_AGE + 5),
        ]
    )
    await _sweep(orch, _MAX_AGE)
    assert orch.cancelled == ["stalled"]


async def test_boundary_exactly_at_max_age_is_not_cancelled() -> None:
    orch = _StubOrchestrator([_task("t1", heartbeat_age=_MAX_AGE - 1)])
    await _sweep(orch, _MAX_AGE)
    assert orch.cancelled == []


async def test_answering_a_day_old_card_does_not_get_the_task_cancelled_as_stuck() -> None:
    """The wait was skipped but not discounted: the next sweep after your answer measured the whole day."""
    orch = _StubOrchestrator([_task("t1", heartbeat_age=_MAX_AGE * 2)], waiting_on_you=frozenset({"t1"}))
    await _sweep(orch, _MAX_AGE)

    orch._waiting_on_you = frozenset()  # you answered; the task works again
    await _sweep(orch, _MAX_AGE)

    assert orch.cancelled == []
