"""Unit tests for the stuck-task watchdog sweep (orchestrator/watchdog.py)."""

from __future__ import annotations

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


class _StubOrchestrator:
    def __init__(self, tasks: list[RunningTask]) -> None:
        self._store = _StubStore(tasks)
        self.cancelled: list[str] = []

    @property
    def running_task_store(self) -> _StubStore:
        return self._store

    async def cancel_stuck_task(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


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
