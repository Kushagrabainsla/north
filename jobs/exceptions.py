"""Job-queue exceptions."""

from __future__ import annotations

from exceptions import NorthError


class JobError(NorthError):
    """Base class for job-queue failures."""


class JobNotFoundError(JobError):
    """Raised when a job_id does not exist in the queue."""


class JobProcessingError(JobError):
    """Raised when a job cannot be inserted, claimed, or transitioned."""
