"""Models and enums for the persistent job queue. See README Section 11."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, Field

from utils.time import format_local


class JobType(StrEnum):
    """How a job entered the queue. See README 11.1."""

    CRON = "cron"
    EVENT = "event"
    ASYNC = "async"
    RETRY = "retry"


class JobStatus(StrEnum):
    """Lifecycle status of a job. See README 11.1."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Failed, and not safe to retry automatically. A step that submits something
    # on your behalf half-fails constantly - session expired, captcha, submit
    # clicked but the response lost - and a retry then applies twice. Applying
    # twice is worse than not applying, so these surface to you and are retried
    # by hand rather than disappearing into a log or silently repeating.
    NEEDS_ATTENTION = "needs_attention"


class JobPriority(int, Enum):
    """Polled in priority order; lower number = higher priority."""

    HIGH = 1
    MEDIUM = 2
    LOW = 3


class Job(BaseModel):
    """One persistent job in the queue. Schema mirrors README 11.1."""

    job_id: str
    type: JobType
    agent: str
    task: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    priority: JobPriority = JobPriority.MEDIUM
    scheduled_at: datetime

    started_at: datetime | None = None
    completed_at: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    retry_after: datetime | None = None
    created_at: datetime | None = None


# A run that starts this long after its slot is late, and says so beside the slot.
_LATE_AFTER = timedelta(minutes=2)


def late_note(job: Job) -> str:
    """When a run started after its slot: "ran 08:58, 1 h 58 min late: the computer was asleep".

    Empty for a run on time or not started. Listed by its slot alone, a briefing
    that ran two hours late read as having run on time.
    """
    if job.started_at is None or job.started_at - job.scheduled_at <= _LATE_AFTER:
        return ""
    minutes = round((job.started_at - job.scheduled_at).total_seconds() / 60)
    hours, rest = divmod(minutes, 60)
    by = f"{minutes} min" if not hours else f"{hours} h {rest} min" if rest else f"{hours} h"
    note = f"ran {format_local(job.started_at, '%H:%M')}, {by} late"
    reason = (job.payload or {}).get("late_because")
    return f"{note}: {reason}" if reason else note


def display_job_status(job: Job) -> str:
    """Translate queue internals into the lifecycle language shown to people."""
    if job.status is JobStatus.PENDING:
        return "retrying" if job.retry_count > 0 else "queued"
    return job.status.value
