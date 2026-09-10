"""Shared vocabulary for the schedule tools (create / list / update / cancel).

One place decides what a schedule is called, how it is described back to the
user, and how a time given in words becomes a stored one - so the four tools
cannot drift from each other or from `north cron`.
"""

from __future__ import annotations

from typing import Any

from jobs.cron_store import schedule_name
from jobs.models import Job, JobStatus
from jobs.scheduler import (
    V1_CRON_ENTRIES,
    CronEntry,
    next_firing_epoch,
)
from utils.time import format_local, local_timezone_name, resolve_timezone
from utils.weekdays import parse_weekdays

__all__ = [
    "BUILTIN_NAMES",
    "builtin_views",
    "describe_weekdays",
    "entry_view",
    "is_pending_one_shot",
    "job_view",
    "parse_weekdays",
    "resolve_zone_name",
    "schedule_name",
]

# Schedules that ship with north. They are shown so "what is scheduled?" has a
# complete answer, and refused for update/cancel because they are part of the
# install, not of the user's own list.
BUILTIN_NAMES = frozenset(entry.name for entry in V1_CRON_ENTRIES)


# ``parse_weekdays`` now lives in the platform layer (``utils.weekdays``) so an
# HTTP route can read a day selection without importing a tool. It is imported
# above and re-exported here (see ``__all__``) so the schedule tools that have
# always called ``tools.universal._schedules.parse_weekdays`` keep working.


def describe_weekdays(weekdays: frozenset[int] | None) -> str:
    """The cadence in words, for a message back to the user."""
    return CronEntry(name="_", agent="_", task="_", hour=0, minute=0, weekdays=weekdays).cadence


def resolve_zone_name(tz: str | None) -> str:
    """Return a stored zone name: the one given if real, else the machine's own."""
    if not tz:
        return local_timezone_name()
    resolved = resolve_timezone(tz)
    return tz if getattr(resolved, "key", None) == tz else local_timezone_name()


def entry_view(row: dict[str, Any], source: str | None = None) -> dict[str, Any]:
    """Render one stored recurring entry for a tool result: what, when, next.

    A stored row whose name matches a built-in is an *edit* of that built-in, not
    a schedule of the user's own, and says so - otherwise editing the daily
    briefing would report it as something the user had created.
    """
    return _view(
        CronEntry.from_row(row),
        source or ("builtin" if row["name"] in BUILTIN_NAMES else "user"),
    )


def builtin_views() -> list[dict[str, Any]]:
    """Render the schedules north ships with, in the same shape as the user's."""
    return [_view(entry, "builtin") for entry in V1_CRON_ENTRIES]


def _view(entry: CronEntry, source: str) -> dict[str, Any]:
    next_epoch = next_firing_epoch(entry)
    return {
        "name": entry.name,
        "label": entry.label,
        "title": entry.title,
        "task": entry.task,
        "agent": entry.agent,
        "schedule": entry.describe(),
        "cadence": entry.cadence,
        "hour": entry.hour,
        "minute": entry.minute,
        "weekdays": sorted(entry.weekdays) if entry.weekdays else [],
        "tz": entry.zone_name,
        "enabled": entry.enabled,
        # A paused entry has no next run; reporting the time it would have fired
        # reads as a promise north is not making.
        "next_run": format_local(next_epoch) if entry.enabled else "paused",
        "next_run_epoch": next_epoch if entry.enabled else None,
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
