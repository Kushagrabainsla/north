"""Job-queue exceptions."""

from __future__ import annotations

from exceptions import NorthError


class JobError(NorthError):
    """Base class for job-queue failures."""


class JobNotFoundError(JobError):
    """Raised when a job_id does not exist in the queue."""


class JobProcessingError(JobError):
    """Raised when a job cannot be inserted, claimed, or transitioned."""


class JobNeedsAttention(JobError):
    """Raised when retrying a job automatically would be unsafe or exhausted."""


class JobCancelled(JobError):
    """Raised when the work behind a running job was deliberately cancelled."""
