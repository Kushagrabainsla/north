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


@pytest.mark.asyncio
async def test_a_paused_task_that_runs_again_streams_again() -> None:
    """A pause ends the stream; left ended, a resumed task replayed its history and closed, showing nothing live."""
    stream = EventStreamManager()
    await stream.emit("task-1", "token", {"text": "before the pause"})
    await stream.emit_done("task-1")

    assert stream.reopen("task-1") is True
    events = stream.subscribe("task-1")
    replayed = await anext(events)
    await stream.emit("task-1", "token", {"text": "after the resume"})
    live = await anext(events)

    assert "before the pause" in replayed
    assert "after the resume" in live
    await events.aclose()


def test_a_task_still_streaming_needs_no_reopening() -> None:
    assert EventStreamManager().reopen("task-1") is False
