"""Task lifecycle: submit, inspect, cancel, pause, resume."""

from __future__ import annotations

from fastapi import HTTPException, Request

from jobs.models import JobStatus
from orchestrator.api.deps import _get_job_processor, _get_orchestrator, router
from orchestrator.models import TaskRequest, TaskResponse


@router.post("/task", response_model=TaskResponse, status_code=202)
async def submit_task(request: Request) -> TaskResponse:
    """Submit a new task for processing. Accepts JSON or form-encoded bodies."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        task_req = TaskRequest(prompt=str(form.get("prompt", "")))
    else:
        body = await request.json()
        task_req = TaskRequest(**body)
    return await _get_orchestrator().submit_task(task_req)


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks() -> list[TaskResponse]:
    """List all currently pending tasks."""
    return await _get_orchestrator().list_active_tasks()


@router.get("/task/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str) -> TaskResponse:
    """Get the status and most recent output for a specific task."""
    result = await _get_orchestrator().get_task(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return result


@router.delete("/task/{task_id}", status_code=204)
async def cancel_task(task_id: str) -> None:
    """Cancel a pending task."""
    cancelled = await _get_orchestrator().cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not in flight - nothing to cancel.")


@router.post("/task/{task_id}/pause")
async def pause_task(task_id: str) -> dict[str, str]:
    """Pause a running task. The task stops but can be resumed later."""
    paused = await _get_orchestrator().pause_task(task_id)
    if not paused:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not in flight - nothing to pause.")
    return {"status": "paused", "task_id": task_id}


@router.post("/task/{task_id}/resume")
async def resume_task(task_id: str) -> dict[str, str]:
    """Resume a previously paused task."""
    resumed = await _get_orchestrator().resume_paused_task(task_id)
    if not resumed:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not paused or already in flight.")
    return {"status": "resumed", "task_id": task_id}


@router.post("/cancel-all")
async def cancel_all() -> dict[str, int]:
    """Stop everything in flight: cancel all active tasks and all pending jobs."""
    tasks_cancelled = await _get_orchestrator().cancel_all_tasks()
    processor = _get_job_processor()
    jobs_cancelled = 0
    for job in await processor.list_jobs(status=JobStatus.PENDING, limit=1000):
        await processor.cancel(job.job_id)
        jobs_cancelled += 1
    return {"tasks_cancelled": tasks_cancelled, "jobs_cancelled": jobs_cancelled}


@router.post("/cancel/{target_id}")
async def cancel_any(target_id: str) -> dict[str, str]:
    """Cancel one thing by id, whether it's an active task or a pending/running job."""
    if await _get_orchestrator().cancel_task(target_id):
        return {"cancelled": "task", "id": target_id}
    processor = _get_job_processor()
    job = await processor.get(target_id)
    if job is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        await processor.cancel(target_id)
        return {"cancelled": "job", "id": target_id}
    raise HTTPException(
        status_code=404, detail=f"{target_id!r} is not an active task or a pending/running job."
    )


