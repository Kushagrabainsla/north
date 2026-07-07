"""Tests for UpdatePlanTool (#9 plan-and-track)."""

from __future__ import annotations

import pytest

from orchestrator.plan_store import PlanStore
from tools.models import ToolInput
from tools.universal.update_plan import UpdatePlanTool


class _RecordingStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, task_id: str, event: str, data: dict) -> None:
        self.events.append((task_id, event, data))


@pytest.mark.asyncio
async def test_update_plan_writes_store_and_returns_rendered():
    store = PlanStore()
    tool = UpdatePlanTool(plan_store=store)
    out = await tool.run(
        ToolInput(
            params={
                "task_id": "t1",
                "steps": [
                    {"content": "step one", "status": "done"},
                    {"content": "step two", "status": "in_progress"},
                ],
            }
        )
    )
    assert out.success
    assert out.data["done"] == 1
    assert out.data["total"] == 2
    assert "[x] step one" in out.data["plan"]
    assert store.progress("t1") == (1, 2)


@pytest.mark.asyncio
async def test_update_plan_emits_stream_event():
    store = PlanStore()
    stream = _RecordingStream()
    tool = UpdatePlanTool(plan_store=store, stream_manager=stream)
    await tool.run(ToolInput(params={"task_id": "t1", "steps": [{"content": "x"}]}))
    assert stream.events
    task_id, event, data = stream.events[0]
    assert task_id == "t1"
    assert event == "plan_updated"
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_update_plan_requires_task_id():
    tool = UpdatePlanTool(plan_store=PlanStore())
    out = await tool.run(ToolInput(params={"steps": [{"content": "x"}]}))
    assert not out.success
    assert "task_id" in (out.error or "")


@pytest.mark.asyncio
async def test_update_plan_rejects_non_list_steps():
    tool = UpdatePlanTool(plan_store=PlanStore())
    out = await tool.run(ToolInput(params={"task_id": "t1", "steps": "nope"}))
    assert not out.success
    assert "list" in (out.error or "")


@pytest.mark.asyncio
async def test_update_plan_survives_stream_failure():
    class _BoomStream:
        async def emit(self, *a, **k):
            raise RuntimeError("ui down")

    store = PlanStore()
    tool = UpdatePlanTool(plan_store=store, stream_manager=_BoomStream())
    out = await tool.run(ToolInput(params={"task_id": "t1", "steps": [{"content": "x"}]}))
    assert out.success  # streaming is best-effort
    assert store.render("t1") == "[ ] x"
