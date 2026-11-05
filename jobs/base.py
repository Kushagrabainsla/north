"""Abstract interface for the persistent job queue. See README Section 11."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from jobs.models import Job, JobStatus


class JobProcessor(ABC):
    """Persistent job queue: CRUD + atomic claim.

    The processor stores jobs and hands them out to a runner. It does NOT
    execute jobs itself - actual agent dispatch lives in the Orchestrator
    (README Section 11.2).
    """

    @abstractmethod
    async def enqueue(self, job: Job) -> str:
        """Insert a new job. Returns job_id.

        Raises:
            JobProcessingError: if the job cannot be inserted (e.g. duplicate id).
        """

    @abstractmethod
    async def get(self, job_id: str) -> Job | None:
        """Return one job by id, or None if not found."""

    @abstractmethod
    async def claim_next(self) -> Job | None:
        """Atomically claim the next eligible job.

        Eligible = status `pending`, `scheduled_at <= now`, and either
        `retry_after IS NULL` or `retry_after <= now`. Ordered by priority
        ascending then scheduled_at ascending. Claimed jobs transition to
        `running` with `started_at` set. Returns None if nothing is eligible.
        """

    @abstractmethod
    async def mark_completed(self, job_id: str) -> None:
        """Transition `job_id` to `completed` with `completed_at` set."""

    @abstractmethod
    async def mark_failed(self, job_id: str, retry_after: datetime | None = None) -> None:
        """Transition `job_id` to `failed`. If `retry_after` is set, the job
        remains in the queue and becomes re-eligible at that time."""

    @abstractmethod
    async def cancel(self, job_id: str) -> None:
        """Transition `job_id` to `cancelled`. No-op if already terminal."""

    @abstractmethod
    async def list_jobs(self, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        """List jobs, optionally filtered by status, ordered by scheduled_at desc."""

    @abstractmethod
    async def has_active_job(self, agent: str, task: str) -> bool:
        """Return True if a pending or running job already exists for (agent, task).

        Backs the cron scheduler's duplicate-run guard so a firing is skipped
        while a prior run of the same entry is still in flight.
        """

    @abstractmethod
    async def run(
        self,
        on_job: Callable[[Job], Awaitable[Any]] | None = None,
        poll_interval_seconds: int = 5,
    ) -> None:
        """Poll the queue forever, dispatching each claimed job to `on_job`.

        Runs until cancelled. `on_job` is called in a fire-and-forget task so
        the poll loop never blocks on job execution. If `on_job` is None, claimed
        jobs are immediately marked completed (no-op drain mode).
        """
