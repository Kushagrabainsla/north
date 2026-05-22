"""FastAPI APIRouter for the Orchestrator endpoints.

See docs/CODING_STYLE.md Sections 12.1–12.4.
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from orchestrator.models import TaskRequest, TaskResponse
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream import EventStreamManager
from utils.security import verify_request_secret

router = APIRouter(
    prefix="/orchestrator",
    tags=["orchestrator"],
    dependencies=[Depends(verify_request_secret)],
)

# Module-level singletons injected by app.py at startup
_orchestrator: Orchestrator | None = None
_stream_manager: EventStreamManager | None = None


def configure(orchestrator: Orchestrator, stream_manager: EventStreamManager) -> None:
    """Wire the singletons used by every route. Called once in app lifespan."""
    global _orchestrator, _stream_manager
    _orchestrator = orchestrator
    _stream_manager = stream_manager


def _get_orchestrator() -> Orchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not configured. Call configure() first.")
    return _orchestrator


def _get_stream_manager() -> EventStreamManager:
    if _stream_manager is None:
        raise RuntimeError("EventStreamManager not configured. Call configure() first.")
    return _stream_manager


@router.post("/task", response_model=TaskResponse, status_code=202)
async def submit_task(request: TaskRequest) -> TaskResponse:
    """Submit a new task for processing."""
    return await _get_orchestrator().submit_task(request)


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks() -> list[TaskResponse]:
    """List all currently pending tasks."""
    return await _get_orchestrator().list_active_tasks()


@router.get("/stream/{task_id}")
async def stream_task_events(task_id: str) -> StreamingResponse:
    """Server-Sent Events stream for real-time task progress."""

    async def _event_generator() -> AsyncIterator[str]:
        async for chunk in _get_stream_manager().subscribe(task_id):
            yield chunk

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
