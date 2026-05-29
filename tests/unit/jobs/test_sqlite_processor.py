"""Tests for SQLiteJobProcessor — enqueue, get, claim_next, mark_*, cancel, list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jobs import (
    Job,
    JobPriority,
    JobProcessingError,
    JobStatus,
    JobType,
    SQLiteJobProcessor,
)


@pytest.fixture
def processor(tmp_path: Path) -> SQLiteJobProcessor:
    return SQLiteJobProcessor(tmp_path / "jobs.db")


def _job(
    job_id: str = "j1",
    *,
    type_: JobType = JobType.CRON,
    agent: str = "health",
    task: str = "meal_plan",
    priority: JobPriority = JobPriority.MEDIUM,
    scheduled_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> Job:
    return Job(
        job_id=job_id,
        type=type_,
        agent=agent,
        task=task,
        priority=priority,
        scheduled_at=scheduled_at or datetime.now(UTC),
        payload=payload or {},
    )


async def test_enqueue_then_get_round_trips(processor: SQLiteJobProcessor) -> None:
    original = _job(payload={"meals": 3, "vegetarian": True})
    await processor.enqueue(original)

    fetched = await processor.get("j1")

    assert fetched is not None
    assert fetched.job_id == "j1"
    assert fetched.type is JobType.CRON
    assert fetched.payload == {"meals": 3, "vegetarian": True}
    assert fetched.status is JobStatus.PENDING


async def test_get_returns_none_for_missing(processor: SQLiteJobProcessor) -> None:
    assert await processor.get("does-not-exist") is None


async def test_enqueue_rejects_duplicate_id(processor: SQLiteJobProcessor) -> None:
    await processor.enqueue(_job("dup"))
    with pytest.raises(JobProcessingError):
        await processor.enqueue(_job("dup"))


async def test_claim_next_returns_none_when_queue_empty(
    processor: SQLiteJobProcessor,
) -> None:
    assert await processor.claim_next() is None


async def test_claim_next_marks_job_running(processor: SQLiteJobProcessor) -> None:
    await processor.enqueue(_job("j1"))

    claimed = await processor.claim_next()

    assert claimed is not None
    assert claimed.job_id == "j1"
    assert claimed.status is JobStatus.RUNNING
    assert claimed.started_at is not None


async def test_claim_next_respects_priority_then_scheduled_at(
    processor: SQLiteJobProcessor,
) -> None:
    now = datetime.now(UTC)
    await processor.enqueue(_job("low", priority=JobPriority.LOW, scheduled_at=now - timedelta(minutes=10)))
    await processor.enqueue(_job("high-later", priority=JobPriority.HIGH, scheduled_at=now))
    await processor.enqueue(_job("medium", priority=JobPriority.MEDIUM, scheduled_at=now - timedelta(minutes=5)))

    first = await processor.claim_next()
    second = await processor.claim_next()
    third = await processor.claim_next()

    assert [first.job_id, second.job_id, third.job_id] == ["high-later", "medium", "low"]


async def test_claim_next_skips_future_scheduled_jobs(
    processor: SQLiteJobProcessor,
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    await processor.enqueue(_job("future", scheduled_at=future))

    assert await processor.claim_next() is None


async def test_claim_next_respects_retry_after(processor: SQLiteJobProcessor) -> None:
    await processor.enqueue(_job("j1"))
    await processor.mark_failed(
        "j1", retry_after=datetime.now(UTC) + timedelta(hours=1)
    )

    assert await processor.claim_next() is None


async def test_mark_completed_transitions_status(
    processor: SQLiteJobProcessor,
) -> None:
    await processor.enqueue(_job("j1"))
    await processor.claim_next()
    await processor.mark_completed("j1")

    fetched = await processor.get("j1")
    assert fetched is not None
    assert fetched.status is JobStatus.COMPLETED
    assert fetched.completed_at is not None


async def test_mark_failed_without_retry_is_terminal(
    processor: SQLiteJobProcessor,
) -> None:
    await processor.enqueue(_job("j1"))
    await processor.claim_next()
    await processor.mark_failed("j1")

    fetched = await processor.get("j1")
    assert fetched is not None
    assert fetched.status is JobStatus.FAILED


async def test_mark_failed_with_retry_after_keeps_job_pending(
    processor: SQLiteJobProcessor,
) -> None:
    await processor.enqueue(_job("j1"))
    await processor.claim_next()
    retry_at = datetime.now(UTC) + timedelta(minutes=2)
    await processor.mark_failed("j1", retry_after=retry_at)

    fetched = await processor.get("j1")
    assert fetched is not None
    assert fetched.status is JobStatus.PENDING
    assert fetched.retry_count == 1
    assert fetched.retry_after is not None


async def test_cancel_transitions_pending_job(processor: SQLiteJobProcessor) -> None:
    await processor.enqueue(_job("j1"))
    await processor.cancel("j1")

    fetched = await processor.get("j1")
    assert fetched is not None
    assert fetched.status is JobStatus.CANCELLED


async def test_cancel_does_not_change_completed_job(
    processor: SQLiteJobProcessor,
) -> None:
    await processor.enqueue(_job("j1"))
    await processor.claim_next()
    await processor.mark_completed("j1")

    await processor.cancel("j1")

    fetched = await processor.get("j1")
    assert fetched is not None
    assert fetched.status is JobStatus.COMPLETED


async def test_list_jobs_filters_by_status(processor: SQLiteJobProcessor) -> None:
    await processor.enqueue(_job("p1"))
    await processor.enqueue(_job("p2"))
    await processor.enqueue(_job("c1"))
    await processor.cancel("c1")

    pending = await processor.list_jobs(status=JobStatus.PENDING)
    cancelled = await processor.list_jobs(status=JobStatus.CANCELLED)

    assert {j.job_id for j in pending} == {"p1", "p2"}
    assert {j.job_id for j in cancelled} == {"c1"}


async def test_list_jobs_respects_limit(processor: SQLiteJobProcessor) -> None:
    for i in range(5):
        await processor.enqueue(_job(f"j{i}"))

    results = await processor.list_jobs(limit=2)

    assert len(results) == 2
