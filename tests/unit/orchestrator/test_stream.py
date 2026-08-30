from __future__ import annotations

import pytest

import orchestrator.stream as stream_module
from orchestrator.stream import EventStreamManager


@pytest.mark.asyncio
async def test_task_stream_sends_heartbeat_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_module, "_SSE_HEARTBEAT_SECONDS", 0.01)
    stream = EventStreamManager()
    events = stream.subscribe("task-1")

    heartbeat = await anext(events)

    assert heartbeat == ": keep-alive\n\n"
    await events.aclose()


@pytest.mark.asyncio
async def test_global_stream_sends_heartbeat_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_module, "_SSE_HEARTBEAT_SECONDS", 0.01)
    stream = EventStreamManager()
    events = stream.subscribe_global()

    heartbeat = await anext(events)

    assert heartbeat == ": keep-alive\n\n"
    await events.aclose()
