"""User-defined recurring schedules."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from orchestrator.api.deps import _get_cron_store, router


class CronEntryOut(BaseModel):
    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekday: int | None


class CronEntryCreate(BaseModel):
    name: str
    agent: str = "general"
    task: str
    hour: int
    minute: int = 0
    weekday: int | None = None


@router.get("/cron", response_model=list[CronEntryOut])
async def list_cron_entries() -> list[CronEntryOut]:
    """List user-defined recurring schedules."""
    entries = await _get_cron_store().list()
    return [CronEntryOut(**e) for e in entries]


@router.post("/cron", response_model=CronEntryOut, status_code=201)
async def create_cron_entry(body: CronEntryCreate) -> CronEntryOut:
    """Add a new recurring schedule."""
    if not (0 <= body.hour <= 23):
        raise HTTPException(status_code=422, detail="hour must be 0-23")
    if not (0 <= body.minute <= 59):
        raise HTTPException(status_code=422, detail="minute must be 0-59")
    if body.weekday is not None and not (0 <= body.weekday <= 6):
        raise HTTPException(status_code=422, detail="weekday must be 0-6 or null")
    await _get_cron_store().add(
        name=body.name,
        agent=body.agent,
        task=body.task,
        hour=body.hour,
        minute=body.minute,
        weekday=body.weekday,
    )
    return CronEntryOut(**body.model_dump())


@router.delete("/cron/{name}", status_code=204)
async def delete_cron_entry(name: str) -> None:
    """Remove a user-defined recurring schedule by name."""
    await _get_cron_store().remove(name)


