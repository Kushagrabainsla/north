"""Server-Sent Events streams for task progress."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

from orchestrator.api.deps import _get_stream_manager, router


@router.get("/stream/{task_id}")
async def stream_task_events(task_id: str) -> StreamingResponse:
    """Server-Sent Events stream for real-time task progress."""

    async def _event_generator() -> AsyncIterator[str]:
        async for chunk in _get_stream_manager().subscribe(task_id):
            yield chunk

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/stream")
async def stream_global_events() -> StreamingResponse:
    """Global SSE stream - all events across all tasks.

    Used by the TUI to receive a single persistent connection for every task
    without needing to subscribe per task_id.
    """

    async def _global_generator() -> AsyncIterator[str]:
        async for chunk in _get_stream_manager().subscribe_global():
            yield chunk

    return StreamingResponse(
        _global_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


