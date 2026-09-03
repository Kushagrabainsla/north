"""Shared vocabulary for the schedule tools (create / list / update / cancel).

One place decides what a schedule is called, how it is described back to the
user, and how a time given in words becomes a stored one - so the four tools
cannot drift from each other or from `north cron`.
"""

from __future__ import annotations

from typing import Any

from jobs.cron_store import schedule_name
from jobs.models import Job, JobStatus
from jobs.scheduler import V1_CRON_ENTRIES, CronEntry, next_firing_epoch
from utils.time import format_local, local_timezone_name, resolve_timezone

__all__ = [
    "BUILTIN_NAMES",
    "builtin_views",
    "entry_view",
    "is_pending_one_shot",
    "job_view",
    "resolve_zone_name",
    "schedule_name",
]

# Schedules that ship with north. They are shown so "what is scheduled?" has a
# complete answer, and refused for update/cancel because they are part of the
# install, not of the user's own list.
BUILTIN_NAMES = frozenset(entry.name for entry in V1_CRON_ENTRIES)


def resolve_zone_name(tz: str | None) -> str:
    """Return a stored zone name: the one given if real, else the machine's own."""
    if not tz:
        return local_timezone_name()
    resolved = resolve_timezone(tz)
    return tz if getattr(resolved, "key", None) == tz else local_timezone_name()


def entry_view(row: dict[str, Any], source: str = "user") -> dict[str, Any]:
    """Render one stored recurring entry for a tool result: what, when, next."""
    return _view(CronEntry.from_row(row), source)


def builtin_views() -> list[dict[str, Any]]:
    """Render the schedules north ships with, in the same shape as the user's."""
    return [_view(entry, "builtin") for entry in V1_CRON_ENTRIES]


def _view(entry: CronEntry, source: str) -> dict[str, Any]:
    next_epoch = next_firing_epoch(entry)
    return {
        "name": entry.name,
        "task": entry.task,
        "agent": entry.agent,
        "schedule": entry.describe(),
        "next_run": format_local(next_epoch),
        "next_run_epoch": next_epoch,
        "source": source,
    }


def job_view(job: Job) -> dict[str, Any]:
    """Render one pending one-shot job in the same shape as a recurring entry."""
    return {
        "job_id": job.job_id,
        "task": job.task,
        "agent": job.agent,
        "schedule": "once",
        "next_run": format_local(job.scheduled_at),
        "next_run_epoch": job.scheduled_at.timestamp(),
    }


def is_pending_one_shot(job: Job) -> bool:
    """A scheduled job still waiting to run, as opposed to a cron firing or history."""
    return job.status is JobStatus.PENDING and (job.payload or {}).get("scheduled_by") is not None
