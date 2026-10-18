"""Tests for jobs models and enums (README Section 11.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import utils.time as time_utils
from jobs import Job, JobPriority, JobStatus, JobType, display_job_status
from jobs.models import late_note


def test_job_type_enum_matches_spec() -> None:
    assert {t.value for t in JobType} == {"cron", "event", "async", "retry"}


def test_job_status_enum_matches_spec() -> None:
    assert {s.value for s in JobStatus} == {
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        # Failed and deliberately not retried - a step that submits something
        # cannot be safely repeated. Surfaces to the user instead.
        "needs_attention",
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
        scheduled_at=datetime.now(UTC),
    )
    assert job.status is JobStatus.PENDING
    assert job.priority is JobPriority.MEDIUM
    assert job.retry_count == 0
    assert job.max_retries == 3
    assert job.payload == {}
    assert job.retry_after is None


def test_job_display_status_distinguishes_first_queue_from_retry() -> None:
    job = Job(
        job_id="j1",
        type=JobType.CRON,
        agent="general",
        task="brief",
        scheduled_at=datetime.now(UTC),
    )
    assert display_job_status(job) == "queued"
    assert display_job_status(job.model_copy(update={"retry_count": 1})) == "retrying"


def _briefing(started: datetime | None, late_because: str = "") -> Job:
    return Job(
        job_id="j1",
        type=JobType.CRON,
        agent="general",
        task="Compile and save the daily news briefing.",
        payload={"cron_entry": "news", **({"late_because": late_because} if late_because else {})},
        scheduled_at=datetime(2026, 10, 17, 14, 0, tzinfo=UTC),  # 07:00 in Los Angeles
        started_at=started,
    )


@pytest.mark.parametrize(
    ("started", "late_because", "note"),
    [
        # Oct 17, lid closed: the briefing ran at 08:58 and was listed at 07:00.
        (
            datetime(2026, 10, 17, 15, 58, tzinfo=UTC),
            "the computer was asleep",
            "ran 08:58, 1 h 58 min late: the computer was asleep",
        ),
        (datetime(2026, 10, 17, 14, 47, tzinfo=UTC), "", "ran 07:47, 47 min late"),
        (
            datetime(2026, 10, 17, 16, 0, tzinfo=UTC),
            "north was not running",
            "ran 09:00, 2 h late: north was not running",
        ),
        (datetime(2026, 10, 17, 14, 1, tzinfo=UTC), "", ""),  # a minute's delay is on time
        (None, "", ""),  # not started yet
    ],
)
def test_a_late_run_says_when_it_ran_and_why(monkeypatch, started, late_because, note) -> None:
    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)
    time_utils.configure_timezone("America/Los_Angeles")

    assert late_note(_briefing(started, late_because)) == note
