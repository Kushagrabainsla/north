"""The workspace lock covers a change, never a wait for a person or a run a tool starts.

The agent loop held a folder's lock around the whole tool call. In the issue #76
test a card left waiting in one session stalled another session's write to the
same folder until the card was answered (finding A5), and a flow made from chat
waited for ever: create_flow's test run needed the lock for its own `bash` step,
held by the agent that called create_flow (M3).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from agents.models import AgentPayload
from config.approval_mode import ApprovalMode
from inference.models import ToolCall
from tests.conftest import bind_approvals
from tests.unit.agents.test_react_loop import _load_agent
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.create_flow import CreateFlowTool
from tools.universal.run_flow import RunFlowTool


class _Saves(Tool):
    name = "save_note"
    description = "Writes a note."
    is_mutating = True
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self, spans: list[tuple[str, str]] | None = None) -> None:
        self.spans = spans if spans is not None else []

    async def run(self, input: ToolInput) -> ToolOutput:
        self.spans.append(("start", str(input.params.get("call"))))
        await asyncio.sleep(0.05)
        self.spans.append(("end", str(input.params.get("call"))))
        return ToolOutput(success=True, data={"saved": True})


class _WaitsForYou(_Saves):
    name = "copy_outside"


def _store_that_waits(released: asyncio.Event) -> MagicMock:
    """A card nobody answers until *released*."""
    store = MagicMock()

    async def wait_for_decision(card_id: str) -> MagicMock:
        await released.wait()
        return MagicMock(chosen_option="Reject", status="rejected")

    store.wait_for_decision = wait_for_decision
    return store


def _calls(agent, tool: Tool, workspace: Path, task: str, call: str = "c1"):
    payload = AgentPayload(prompt="p", task_id=task, workspace=str(workspace))
    return agent._execute_calls_ordered([ToolCall(name=tool.name, call_id=call, params={})], payload, {tool.name: tool})


async def test_a_card_waiting_in_one_session_does_not_stall_anothers_write(tmp_path: Path) -> None:
    released = asyncio.Event()
    waiting = bind_approvals(_WaitsForYou(), ApprovalMode.ASK, store=_store_that_waits(released))
    saves = bind_approvals(_Saves(), ApprovalMode.YOLO)
    session_a = asyncio.create_task(_calls(_load_agent("general", tmp_path), waiting, tmp_path, "task-a"))
    await asyncio.sleep(0.2)  # A's card is up and nobody answers it

    try:
        [(_, _, ok, _)] = await asyncio.wait_for(_calls(_load_agent("general", tmp_path), saves, tmp_path, "task-b"), 2)
        assert ok
    finally:
        released.set()
        await session_a


async def test_a_run_a_tool_starts_can_change_the_folder_its_parent_works_in(tmp_path: Path) -> None:
    nested = bind_approvals(_Saves(), ApprovalMode.YOLO)

    class MakesAFlow(Tool):
        """Like create_flow: its test run's step changes the same folder."""

        name = "make_flow"
        description = "Creates a flow and test-runs it."
        is_mutating = True
        starts_runs = True
        parameters_schema = {"type": "object", "properties": {}}

        async def run(self, input: ToolInput) -> ToolOutput:
            [(_, _, ok, _)] = await _calls(_load_agent("general", tmp_path), nested, tmp_path, "task-a", "step")
            return ToolOutput(success=ok, data={})

    maker = bind_approvals(MakesAFlow(), ApprovalMode.YOLO)

    [(_, _, ok, _)] = await asyncio.wait_for(_calls(_load_agent("general", tmp_path), maker, tmp_path, "task-a"), 2)

    assert ok


def test_the_flow_tools_say_they_start_runs() -> None:
    assert CreateFlowTool.starts_runs and RunFlowTool.starts_runs


async def test_two_changes_to_one_folder_still_never_overlap(tmp_path: Path) -> None:
    spans: list[tuple[str, str]] = []
    tool = bind_approvals(_Saves(spans), ApprovalMode.YOLO)

    await asyncio.gather(
        tool.execute(ToolInput(params={"call": "1", "workspace": str(tmp_path)})),
        tool.execute(ToolInput(params={"call": "2", "workspace": str(tmp_path)})),
    )

    assert [kind for kind, _ in spans] == ["start", "end", "start", "end"]
