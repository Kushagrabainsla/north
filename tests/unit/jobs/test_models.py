"""Tests for jobs models and enums (README Section 11.1)."""

from __future__ import annotations

from datetime import datetime, timezone

from jobs import Job, JobPriority, JobStatus, JobType


def test_job_type_enum_matches_spec() -> None:
    assert {t.value for t in JobType} == {"cron", "event", "async", "retry"}


def test_job_status_enum_matches_spec() -> None:
    assert {s.value for s in JobStatus} == {
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    }


def test_job_priority_values_are_1_2_3() -> None:
    assert int(JobPriority.HIGH) == 1
    assert int(JobPriority.MEDIUM) == 2
    assert int(JobPriority.LOW) == 3


def test_job_accepts_minimal_fields_with_sane_defaults() -> None:
    job = Job(
        job_id="j1",
        type=JobType.CRON,
        agent="health",
        task="meal_plan",
        scheduled_at=datetime.now(timezone.utc),
    )
    assert job.status is JobStatus.PENDING
    assert job.priority is JobPriority.MEDIUM
    assert job.retry_count == 0
    assert job.max_retries == 3
    assert job.payload == {}
    assert job.retry_after is None
