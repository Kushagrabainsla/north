"""Recurring schedules: the user's own, and the built-ins they can retime or pause.

A built-in ships as a constant. Editing one writes a stored row that stands in
for it, so the shipped values are never lost and deleting the row restores them.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from flows.exceptions import FlowNotFoundError
from flows.validation import schedulable_flow_error
from jobs.cron_store import UNSET
from jobs.scheduler import (
    V1_CRON_ENTRIES,
    CronEntry,
    apply_shipped_defaults,
    builtin_default,
    merge_entries,
    next_firing_epoch,
)
from orchestrator.api.deps import _get_cron_store, router
from orchestrator.api_context import current_services
from utils.time import format_local, is_known_timezone, local_timezone_name, now_epoch
from utils.weekdays import parse_weekdays


class CronEntryOut(BaseModel):
    """One schedule. `hour`/`minute` are wall clock in `tz`; the next firing is
    given as both an epoch (for machines) and local text (for people).

    `source` is "user" for a schedule the user created and "builtin" for one
    north ships with. The page shows the two apart: a user's own is editable,
    a built-in is read-only there and explains itself through `description`.
    A built-in additionally reports `modified`, and deleting it restores the
    shipped values rather than removing it.

    `weekdays` is the days it runs, empty meaning every day. `cadence` is the
    same fact in the words a person uses ("weekdays", "weekends", "every Tue"),
    so a client never has to translate a set of integers back into English.
    """

    name: str
    # `name` is the key it is addressed by, `label` the title a person reads, and
    # `task` the prompt that actually runs. One field used to be all three.
    label: str
    title: str
    # What the schedule is for, in a sentence. Only built-ins have one - a
    # routine the user wrote is explained by its own prompt, where a built-in's
    # prompt may be an internal slug that says nothing to the person reading it.
    description: str = ""
    agent: str
    task: str
    # The flow this schedule runs, or empty for an older prompt-only schedule.
    # When set, `task` and `agent` are only labels: the flow carries the work.
    flow: str = ""
    skill: str = ""
    hour: int
    minute: int
    interval_minutes: int | None = None
    anchor_epoch: float | None = None
    weekdays: list[int]
    cadence: str
    enabled: bool
    tz: str
    schedule: str
    next_run_epoch: float
    next_run_local: str
    source: str = "user"
    # True for a built-in the user has edited. The client uses it to offer
    # "restore default", which is the only way back to the shipped values.
    modified: bool = False


class CronEntryCreate(BaseModel):
    """A new schedule. It runs a flow, so all it says is which flow and when."""

    name: str | None = None
    label: str = ""
    flow: str = Field(min_length=1)
    hour: int | None = None
    minute: int = 0
    interval_minutes: int | None = None
    # Accepts day numbers, day names, or "weekdays" / "weekends" / "daily",
    # matching what the schedule_task tool takes - one vocabulary, two doors.
    days: Any = None
    tz: str | None = None
    enabled: bool = True


class CronEntryUpdate(BaseModel):
    """Every field optional: what is not sent is left as it is.

    What a schedule does lives in its flow, so there is no prompt or agent to
    change here - only when it runs, what it is called, and which flow it runs.

    ``days`` is the exception that proves it. Sending ``"daily"`` clears a day
    restriction, which is a real change and not the same as omitting the field.
    """

    label: str | None = None
    flow: str | None = None
    hour: int | None = None
    minute: int | None = None
    interval_minutes: int | None = None
    days: Any = None
    tz: str | None = None
    enabled: bool | None = None


BUILTIN_NAMES = frozenset(entry.name for entry in V1_CRON_ENTRIES)


def _to_out(row: dict) -> CronEntryOut:
    name = row["name"]
    is_builtin = name in BUILTIN_NAMES
    entry = apply_shipped_defaults(CronEntry.from_row(row))
    return _entry_out(entry, "builtin" if is_builtin else "user", modified=is_builtin)


def _entry_out(entry: CronEntry, source: str, *, modified: bool = False) -> CronEntryOut:
    next_epoch = next_firing_epoch(entry)
    return CronEntryOut(
        name=entry.name,
        label=entry.label,
        title=entry.title,
        description=entry.description,
        agent=entry.agent,
        task=entry.task,
        flow=entry.flow,
        skill=entry.skill,
        hour=entry.hour,
        minute=entry.minute,
        interval_minutes=entry.interval_minutes,
        anchor_epoch=entry.anchor_epoch,
        weekdays=sorted(entry.weekdays) if entry.weekdays else [],
        cadence=entry.cadence,
        enabled=entry.enabled,
        tz=entry.zone_name,
        schedule=entry.describe(),
        next_run_epoch=next_epoch,
        next_run_local=format_local(next_epoch, timezone=entry.zone_name),
        source=source,
        modified=modified,
    )


def _validate(hour: int | None, minute: int | None, interval_minutes: int | None = None) -> None:
    if hour is not None and not (0 <= hour <= 23):
        raise HTTPException(status_code=422, detail="hour must be 0-23")
    if minute is not None and not (0 <= minute <= 59):
        raise HTTPException(status_code=422, detail="minute must be 0-59")
    if interval_minutes is not None and interval_minutes < 1:
        raise HTTPException(status_code=422, detail="interval_minutes must be at least 1")


def _timezone(value: str | None) -> str:
    """Resolve an omitted zone to North's setting and reject misspellings."""
    name = value or local_timezone_name()
    if not is_known_timezone(name):
        raise HTTPException(status_code=422, detail=f"Unknown timezone {name!r}")
    return name


def _days(value: Any) -> frozenset[int] | None:
    """Read a day selection, reporting a bad one as a 422 rather than a 500."""
    try:
        return parse_weekdays(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _schedulable_flow(name: str):
    """The flow *name*, once it is known to be safe to run unattended.

    The same question every other door asks - active, unchanged since it was
    tested, still executable - so a schedule made here obeys the rules of one
    made in chat or on the Flows page.
    """
    services = current_services()
    try:
        flow = services.require("flow_registry").get(name)
    except FlowNotFoundError:
        raise HTTPException(status_code=422, detail=f"Unknown flow '{name}'.") from None
    problem = schedulable_flow_error(
        flow,
        skill_registry=services.skill_registry,
        agent_registry=services.agent_registry,
        tool_registry=services.tool_registry,
    )
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    return flow


async def _ensure_editable_row(store, name: str) -> None:
    """Make sure there is a stored row for *name*, so an edit has somewhere to land.

    A built-in ships as a constant in the source. Editing one writes a row that
    stands in for it, seeded from the shipped values so an edit that names one
    field leaves the rest as they were. Deleting the row restores the default.

    This exists because a schedule the user asked north to create - the daily
    briefing - was written into the source rather than stored, and from then on
    the person whose briefing it was could not move it, pause it, or retime it.
    """
    if await store.get(name) is not None:
        return
    default = builtin_default(name)
    if default is None:
        raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
    await store.add(
        name=default.name,
        agent=default.agent,
        task=default.task,
        hour=default.hour,
        minute=default.minute,
        weekdays=default.weekdays,
        tz=default.zone_name,
        enabled=default.enabled,
        label=default.label,
        interval_minutes=default.interval_minutes,
        anchor_epoch=default.anchor_epoch,
    )


@router.get("/cron", response_model=list[CronEntryOut])
async def list_cron_entries(builtin: bool = True) -> list[CronEntryOut]:
    """List recurring schedules, soonest firing first. Pass builtin=false for the user's own.

    A built-in the user has edited is listed once, in its edited form, and marked
    `modified` - listing both the shipped and the stored version would show two
    schedules where one runs.
    """
    rows = await _get_cron_store().list()
    stored = {row["name"] for row in rows}
    merged = merge_entries(list(V1_CRON_ENTRIES) if builtin else [], rows)
    entries = [
        _entry_out(
            entry,
            "builtin" if entry.name in BUILTIN_NAMES else "user",
            modified=entry.name in BUILTIN_NAMES and entry.name in stored,
        )
        for entry in merged
        if builtin or entry.name not in BUILTIN_NAMES or entry.name in stored
    ]
    return sorted(entries, key=lambda e: e.next_run_epoch)


@router.post("/cron", response_model=CronEntryOut, status_code=201)
async def create_cron_entry(body: CronEntryCreate) -> CronEntryOut:
    """Add a recurring schedule. Times are wall clock in `tz` (default: North's configured zone)."""
    _validate(body.hour, body.minute, body.interval_minutes)
    if (body.hour is None) == (body.interval_minutes is None):
        raise HTTPException(status_code=422, detail="provide exactly one of hour or interval_minutes")
    if body.interval_minutes is not None and body.days is not None:
        raise HTTPException(status_code=422, detail="interval_minutes cannot be combined with days")
    flow = _schedulable_flow(body.flow)
    store = _get_cron_store()
    # An unnamed schedule gets a name derived from its title, or else its flow,
    # made unique - two schedules that happen to slug the same must not overwrite
    # one another.
    name = body.name or await store.unique_name(body.label or body.flow)
    await store.add(
        name=name,
        agent="general",
        task=flow.description,
        flow=body.flow,
        hour=body.hour or 0,
        minute=0 if body.interval_minutes is not None else body.minute,
        weekdays=_days(body.days),
        tz=_timezone(body.tz),
        enabled=body.enabled,
        label=body.label,
        interval_minutes=body.interval_minutes,
        anchor_epoch=now_epoch() if body.interval_minutes is not None else None,
    )
    row = await store.get(name)
    if row is None:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=500, detail="schedule was not stored")
    return _to_out(row)


@router.patch("/cron/{name}", response_model=CronEntryOut)
async def update_cron_entry(name: str, body: CronEntryUpdate) -> CronEntryOut:
    """Change some fields of one schedule; omitted fields are left alone."""
    _validate(body.hour, body.minute, body.interval_minutes)
    if body.interval_minutes is not None and (body.hour is not None or body.days is not None):
        raise HTTPException(status_code=422, detail="interval_minutes cannot be combined with hour or days")
    if body.flow is not None:
        _schedulable_flow(body.flow)
    store = _get_cron_store()
    await _ensure_editable_row(store, name)
    changes: dict[str, Any] = body.model_dump(exclude_none=True, exclude={"days"})
    if body.interval_minutes is not None:
        changes.update(hour=0, minute=0, weekdays=None, anchor_epoch=now_epoch())
    elif body.hour is not None:
        # Naming a wall-clock hour is the explicit way to leave interval mode.
        changes.update(interval_minutes=None, anchor_epoch=None)
    if body.tz is not None:
        changes["tz"] = _timezone(body.tz)
    # `days` is translated rather than passed through, and only when the caller
    # sent it: UNSET is how the store tells "leave the days alone" apart from
    # "clear them back to daily", which both look like None on the wire.
    if body.interval_minutes is not None:
        changes["weekdays"] = None
    elif body.days is not None:
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
    """Remove a schedule. For a built-in this restores the shipped default.

    A built-in cannot be removed - it lives in the source - so deleting one
    deletes the *edit*, which is the reversal a person means by it. Stopping a
    built-in is what pausing is for.
    """
    removed = await _get_cron_store().remove(name)
    if removed:
        return
    if name in BUILTIN_NAMES:
        raise HTTPException(
            status_code=409,
            detail=f"{name!r} is a built-in schedule at its default settings. Pause it to stop it.",
        )
    raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
