"""The job queue: list, create, cancel."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from jobs.models import Job, JobPriority, JobStatus, JobType
from orchestrator.api.deps import _get_job_processor, router
from utils.ids import generate_id
from utils.time import utcnow


class JobOut(BaseModel):
    job_id: str
    type: str
    agent: str
    task: str
    status: str
    priority: int
    scheduled_at: str
    created_at: str | None


class JobCreateRequest(BaseModel):
    agent: str
    task: str
    payload: dict[str, Any] = {}
    priority: int = 2
    scheduled_at: str | None = None


def _job_to_out(j: Job) -> JobOut:
    return JobOut(
        job_id=j.job_id,
        type=j.type.value,
        agent=j.agent,
        task=j.task,
        status=j.status.value,
        priority=int(j.priority),
        scheduled_at=j.scheduled_at.isoformat(),
        created_at=j.created_at.isoformat() if j.created_at else None,
    )


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    status: str | None = None,
    limit: int = 50,
) -> list[JobOut]:
    """List job queue entries, optionally filtered by status."""
    js: JobStatus | None = None
    if status is not None:
        try:
            js = JobStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown status {status!r}. Valid: {[s.value for s in JobStatus]}",
            ) from None
    jobs = await _get_job_processor().list_jobs(status=js, limit=limit)
    return [_job_to_out(j) for j in jobs]


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(body: JobCreateRequest) -> JobOut:
    """Create and enqueue a new job."""
    scheduled = datetime.datetime.fromisoformat(body.scheduled_at) if body.scheduled_at else utcnow()
    job = Job(
        job_id=generate_id(),
        type=JobType.ASYNC,
        agent=body.agent,
        task=body.task,
        payload=body.payload,
        priority=JobPriority(body.priority),
        scheduled_at=scheduled,
    )
    await _get_job_processor().enqueue(job)
    return _job_to_out(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(job_id: str) -> None:
    """Cancel a pending or running job."""
    await _get_job_processor().cancel(job_id)


