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
    WEEKDAYS,
    WEEKENDS,
    CronEntry,
    next_firing_epoch,
    normalise_weekdays,
)
from utils.time import format_local, local_timezone_name, resolve_timezone

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


# What a person calls each day, and the shorthands they use for groups of them.
# Read leniently on purpose: a model asked for "every weekday" has a dozen
# reasonable ways to say so, and every one it picks that north rejects turns a
# working feature into an error message.
_DAY_WORDS: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_DAY_GROUPS: dict[str, frozenset[int] | None] = {
    "weekday": WEEKDAYS,
    "weekdays": WEEKDAYS,
    "workday": WEEKDAYS,
    "workdays": WEEKDAYS,
    "weekend": WEEKENDS,
    "weekends": WEEKENDS,
    "daily": None,
    "everyday": None,
    "every day": None,
    "all": None,
}


def parse_weekdays(value: object) -> frozenset[int] | None:
    """Read a weekday selection however the caller chose to say it.

    Accepts a day number, a day name, one of the group words ("weekdays",
    "weekends", "daily"), or a list mixing any of those. Returns None for a
    schedule that runs every day.

    Raises ValueError with a sentence a model can act on. The old code called
    ``int()`` on whatever arrived, so "every weekday" - which reaches a tool as
    a list - failed with "int() argument must be ... not 'list'", which tells
    the reader nothing about days of the week.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in _DAY_GROUPS:
        return _DAY_GROUPS[value.strip().lower()]
    items = value if isinstance(value, list | tuple | set | frozenset) else [value]
    days: set[int] = set()
    for item in items:
        days.update(_one_selection(item))
    return normalise_weekdays(days)


def _one_selection(item: object) -> frozenset[int]:
    """The days named by a single element of a weekday selection."""
    if isinstance(item, bool):  # bool is an int; a True here is a caller error
        raise ValueError(f"{item!r} is not a day of the week")
    if isinstance(item, int):
        if not 0 <= item <= 6:
            raise ValueError(f"day must be 0 (Monday) to 6 (Sunday), got {item}")
        return frozenset({item})
    word = str(item).strip().lower()
    if word in _DAY_WORDS:
        return frozenset({_DAY_WORDS[word]})
    if word in _DAY_GROUPS:
        group = _DAY_GROUPS[word]
        return frozenset(range(7)) if group is None else group
    if word.isdigit():
        return _one_selection(int(word))
    raise ValueError(
        f"{item!r} is not a day of the week. Use 0-6 (Monday to Sunday), a day name "
        f"like 'Tue', or one of: weekdays, weekends, daily."
    )


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
