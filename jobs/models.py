"""Models and enums for the persistent job queue. See README Section 11."""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


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
