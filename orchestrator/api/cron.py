"""Recurring schedules: the user's, and the built-in ones they can see but not edit."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from jobs.cron_store import schedule_name
from jobs.scheduler import V1_CRON_ENTRIES, CronEntry, next_firing_epoch
from orchestrator.api.deps import _get_cron_store, router
from utils.time import format_local, local_timezone_name


class CronEntryOut(BaseModel):
    """One schedule. `hour`/`minute` are wall clock in `tz`; the next firing is
    given as both an epoch (for machines) and local text (for people).

    `source` is "user" for a schedule the user created and "builtin" for one
    north ships with - built-ins are listed so "what is scheduled?" has a
    complete answer, but they are part of the install and cannot be edited.
    """

    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekday: int | None
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
    weekday: int | None = None
    tz: str | None = None


class CronEntryUpdate(BaseModel):
    """Every field optional: what is not sent is left as it is."""

    agent: str | None = None
    task: str | None = None
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    tz: str | None = None


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
        weekday=entry.weekday,
        tz=entry.zone_name,
        schedule=entry.describe(),
        next_run_epoch=next_epoch,
        next_run_local=format_local(next_epoch),
        source=source,
    )


def _validate(hour: int | None, minute: int | None, weekday: int | None) -> None:
    if hour is not None and not (0 <= hour <= 23):
        raise HTTPException(status_code=422, detail="hour must be 0-23")
    if minute is not None and not (0 <= minute <= 59):
        raise HTTPException(status_code=422, detail="minute must be 0-59")
    if weekday is not None and not (0 <= weekday <= 6):
        raise HTTPException(status_code=422, detail="weekday must be 0-6 or null")


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
    _validate(body.hour, body.minute, body.weekday)
    store = _get_cron_store()
    name = body.name or schedule_name(body.task)
    await store.add(
        name=name,
        agent=body.agent,
        task=body.task,
        hour=body.hour,
        minute=body.minute,
        weekday=body.weekday,
        tz=body.tz or local_timezone_name(),
    )
    row = await store.get(name)
    if row is None:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=500, detail="schedule was not stored")
    return _to_out(row)


@router.patch("/cron/{name}", response_model=CronEntryOut)
async def update_cron_entry(name: str, body: CronEntryUpdate) -> CronEntryOut:
    """Change some fields of one schedule; omitted fields are left alone."""
    _validate(body.hour, body.minute, body.weekday)
    _reject_builtin(name)
    store = _get_cron_store()
    if not await store.update(name, **body.model_dump(exclude_none=True)):
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
