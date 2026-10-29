"""A running job you cancel stops, and stays cancelled.

Cancelling a running job only wrote "cancelled" on its row. The job ran on, and
when it ended it wrote "completed" over the cancel, or went back to "pending"
for a retry; the task or flow run it had started was never stopped. Found by
verifying north's Oct 24 self-audit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jobs.models import Job, JobStatus, JobType
from jobs.sqlite_processor import SQLiteJobProcessor
from orchestrator.app import _run_scheduled_task
from utils.time import utcnow


def _job(job_id: str = "j1") -> Job:
    return Job(job_id=job_id, type=JobType.ASYNC, agent="general", task="brief", scheduled_at=utcnow())


async def _until(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


async def _status(processor: SQLiteJobProcessor, job_id: str = "j1") -> JobStatus:
    job = await processor.get(job_id)
    assert job is not None
    return job.status


@pytest.mark.asyncio
async def test_cancelling_a_running_job_stops_its_work_and_it_stays_cancelled(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def work(job: Job) -> None:
        started.set()
        try:
            await asyncio.sleep(0.3)  # finishes soon, so a cancel that does not stop it is overwritten
        except asyncio.CancelledError:
            stopped.set()
            raise

    loop = asyncio.create_task(processor.run(on_job=work, poll_interval_seconds=0))
    try:
        await processor.enqueue(_job())
        await asyncio.wait_for(started.wait(), 5)

        await processor.cancel("j1")

        await asyncio.wait_for(stopped.wait(), 5)
        await asyncio.sleep(0.5)
        assert await _status(processor) is JobStatus.CANCELLED
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end",
    [
        lambda p: p.mark_completed("j1"),
        lambda p: p.mark_failed("j1"),
        lambda p: p.mark_failed("j1", retry_after=datetime.now(UTC) + timedelta(seconds=30)),
        lambda p: p.mark_needs_attention("j1"),
    ],
    ids=["completed", "failed", "retry", "needs_attention"],
)
async def test_a_cancel_is_never_overwritten_by_the_jobs_end(tmp_path, end) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    await processor.enqueue(_job())
    await processor.claim_next()

    await processor.cancel("j1")
    await end(processor)

    assert await _status(processor) is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_a_running_job_still_records_how_it_ended(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    await processor.enqueue(_job())
    await processor.claim_next()

    await processor.mark_completed("j1")

    assert await _status(processor) is JobStatus.COMPLETED


class _Orchestrator:
    """Accepts a task that never ends by itself."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def submit_task(self, request):
        return type("Response", (), {"task_id": "task_1"})()

    def is_waiting_on_you(self, task_id: str) -> bool:
        return False

    async def get_task(self, task_id: str):
        return type("Task", (), {"status": "running"})()

    async def cancel_task(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


async def _stop_watching(job_was_cancelled: bool) -> _Orchestrator:
    orchestrator = _Orchestrator()

    async def job_cancelled() -> bool:
        return job_was_cancelled

    watch = asyncio.create_task(
        _run_scheduled_task(orchestrator, "[scheduled] brief", poll_seconds=0.01, job_cancelled=job_cancelled)
    )
    await asyncio.sleep(0.05)
    watch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watch
    return orchestrator


@pytest.mark.asyncio
async def test_a_cancelled_scheduled_job_cancels_the_task_it_started() -> None:
    orchestrator = await _stop_watching(job_was_cancelled=True)

    assert orchestrator.cancelled == ["task_1"]


@pytest.mark.asyncio
async def test_a_shutdown_leaves_the_task_to_resume() -> None:
    orchestrator = await _stop_watching(job_was_cancelled=False)

    assert orchestrator.cancelled == []


@pytest.mark.asyncio
async def test_cancel_all_cancels_running_jobs_too(tmp_path, monkeypatch) -> None:
    from orchestrator.api import task as task_api

    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    await processor.enqueue(_job("running"))
    await processor.claim_next()
    await processor.enqueue(_job("pending"))

    class NoTasks:
        async def cancel_all_tasks(self) -> int:
            return 0

    monkeypatch.setattr(task_api, "_get_orchestrator", NoTasks)
    monkeypatch.setattr(task_api, "_get_job_processor", lambda: processor)

    assert (await task_api.cancel_all())["jobs_cancelled"] == 2
    assert await _status(processor, "running") is JobStatus.CANCELLED
    assert await _status(processor, "pending") is JobStatus.CANCELLED
