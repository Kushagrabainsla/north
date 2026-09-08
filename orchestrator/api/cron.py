"""Recurring schedules: the user's, and the built-in ones they can see but not edit."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from jobs.cron_store import UNSET
from jobs.scheduler import V1_CRON_ENTRIES, CronEntry, next_firing_epoch
from orchestrator.api.deps import _get_cron_store, router
from tools.universal._schedules import parse_weekdays
from utils.time import format_local, local_timezone_name


class CronEntryOut(BaseModel):
    """One schedule. `hour`/`minute` are wall clock in `tz`; the next firing is
    given as both an epoch (for machines) and local text (for people).

    `source` is "user" for a schedule the user created and "builtin" for one
    north ships with - built-ins are listed so "what is scheduled?" has a
    complete answer, but they are part of the install and cannot be edited.

    `weekdays` is the days it runs, empty meaning every day. `cadence` is the
    same fact in the words a person uses ("weekdays", "weekends", "every Tue"),
    so a client never has to translate a set of integers back into English.
    """

    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekdays: list[int]
    cadence: str
    enabled: bool
    tz: str
    schedule: str
    next_run_epoch: float
    next_run_local: str
    source: str = "user"


class CronEntryCreate(BaseModel):
    name: str | None = None
    agent: str = "general"
    task: str
    hour: int
    minute: int = 0
    # Accepts day numbers, day names, or "weekdays" / "weekends" / "daily",
    # matching what the schedule_task tool takes - one vocabulary, two doors.
    days: Any = None
    tz: str | None = None
    enabled: bool = True


class CronEntryUpdate(BaseModel):
    """Every field optional: what is not sent is left as it is.

    ``days`` is the exception that proves it. Sending ``"daily"`` clears a day
    restriction, which is a real change and not the same as omitting the field.
    """

    agent: str | None = None
    task: str | None = None
    hour: int | None = None
    minute: int | None = None
    days: Any = None
    tz: str | None = None
    enabled: bool | None = None


BUILTIN_NAMES = frozenset(entry.name for entry in V1_CRON_ENTRIES)


def _to_out(row: dict) -> CronEntryOut:
    return _entry_out(CronEntry.from_row(row), "user")


def _entry_out(entry: CronEntry, source: str) -> CronEntryOut:
    next_epoch = next_firing_epoch(entry)
    return CronEntryOut(
        name=entry.name,
        agent=entry.agent,
        task=entry.task,
        hour=entry.hour,
        minute=entry.minute,
        weekdays=sorted(entry.weekdays) if entry.weekdays else [],
        cadence=entry.cadence,
        enabled=entry.enabled,
        tz=entry.zone_name,
        schedule=entry.describe(),
        next_run_epoch=next_epoch,
        next_run_local=format_local(next_epoch),
        source=source,
    )


def _validate(hour: int | None, minute: int | None) -> None:
    if hour is not None and not (0 <= hour <= 23):
        raise HTTPException(status_code=422, detail="hour must be 0-23")
    if minute is not None and not (0 <= minute <= 59):
        raise HTTPException(status_code=422, detail="minute must be 0-59")


def _days(value: Any) -> frozenset[int] | None:
    """Read a day selection, reporting a bad one as a 422 rather than a 500."""
    try:
        return parse_weekdays(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _reject_builtin(name: str) -> None:
    if name in BUILTIN_NAMES:
        raise HTTPException(status_code=409, detail=f"{name!r} is a built-in schedule and cannot be changed")


@router.get("/cron", response_model=list[CronEntryOut])
async def list_cron_entries(builtin: bool = True) -> list[CronEntryOut]:
    """List recurring schedules, soonest firing first. Pass builtin=false for the user's own."""
    entries = [_to_out(e) for e in await _get_cron_store().list()]
    if builtin:
        entries += [_entry_out(e, "builtin") for e in V1_CRON_ENTRIES]
    return sorted(entries, key=lambda e: e.next_run_epoch)


@router.post("/cron", response_model=CronEntryOut, status_code=201)
async def create_cron_entry(body: CronEntryCreate) -> CronEntryOut:
    """Add a new recurring schedule. Times are wall clock in `tz` (default: this machine's)."""
    _validate(body.hour, body.minute)
    store = _get_cron_store()
    # An unnamed schedule gets a name derived from its task, made unique - two
    # reminders whose text happens to slug the same must not overwrite one another.
    name = body.name or await store.unique_name(body.task)
    await store.add(
        name=name,
        agent=body.agent,
        task=body.task,
        hour=body.hour,
        minute=body.minute,
        weekdays=_days(body.days),
        tz=body.tz or local_timezone_name(),
        enabled=body.enabled,
    )
    row = await store.get(name)
    if row is None:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=500, detail="schedule was not stored")
    return _to_out(row)


@router.patch("/cron/{name}", response_model=CronEntryOut)
async def update_cron_entry(name: str, body: CronEntryUpdate) -> CronEntryOut:
    """Change some fields of one schedule; omitted fields are left alone."""
    _validate(body.hour, body.minute)
    _reject_builtin(name)
    store = _get_cron_store()
    changes: dict[str, Any] = body.model_dump(exclude_none=True, exclude={"days"})
    # `days` is translated rather than passed through, and only when the caller
    # sent it: UNSET is how the store tells "leave the days alone" apart from
    # "clear them back to daily", which both look like None on the wire.
    if body.days is not None:
        changes["weekdays"] = _days(body.days)
    else:
        changes["weekdays"] = UNSET
    if not await store.update(name, **changes):
        raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
    row = await store.get(name)
    if row is None:  # pragma: no cover - update reported a row it then lost
        raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
    return _to_out(row)


@router.delete("/cron/{name}", status_code=204)
async def delete_cron_entry(name: str) -> None:
    """Remove a user-defined recurring schedule by name."""
    _reject_builtin(name)
    if not await _get_cron_store().remove(name):
        raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
