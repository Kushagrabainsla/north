"""Every agent step tells the stuck-task watchdog the task is still working.

The heartbeat was beaten only when a stage started, so a task working for hours
through many steps read as stalled since its start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.models import AgentPayload
from inference.models import ToolCall, ToolCallResponse
from tests.conftest import MockInferenceRouter
from tests.unit.agents.test_react_loop import _load_agent


class _Store:
    def __init__(self) -> None:
        self.beats: list[str] = []

    async def heartbeat(self, task_id: str) -> None:
        self.beats.append(task_id)

    async def mark_side_effect(self, task_id: str) -> None:
        pass


class _ThreeSteps(MockInferenceRouter):
    def __init__(self) -> None:
        self.calls = 0

    async def complete_with_tools(self, request, token_callback=None):
        self.calls += 1
        if self.calls < 3:
            return ToolCallResponse(
                type="tool_calls",
                calls=[ToolCall(name="missing_tool", call_id=f"c{self.calls}", params={})],
                model_used="m",
            )
        return ToolCallResponse(type="message", content="done", calls=[], model_used="m")


@pytest.mark.asyncio
async def test_each_step_beats_the_heartbeat(tmp_path: Path) -> None:
    agent = _load_agent("general", tmp_path, _ThreeSteps())
    store = _Store()
    agent._deps.running_task_store = store

    await agent.run(AgentPayload(task_id="t1", prompt="Work through it."))

    assert store.beats == ["t1", "t1", "t1"]
