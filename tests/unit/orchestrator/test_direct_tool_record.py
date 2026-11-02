"""A tool call north makes directly is recorded like one an agent makes.

In the issue #76 test (finding W3) the planner sent "pls cancel order 4471" straight
to the MCP tool. The order was cancelled, yet the task's record held no run, no
tool_called or tool_result event, and a ledger row with ``tools_used: []``: the
chat's Details read "– agents · 0 tool events". A direct call that failed and went
to the agent left nothing at all, and the agent then said "I never sent an off
command" about a call north had made.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from approval import UserInteraction
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from orchestrator.agent_runs import AgentRunStore
from orchestrator.models import ExecutionMode, ExecutionPlan
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream import EventStreamManager
from orchestrator.verification import verify_claims
from tests.conftest import bind_approvals
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from utils.tasks import drain


class _CancelOrder(Tool):
    name = "cancel_order"
    description = "Cancels an order."
    is_mutating = True
    parameters_schema = {"type": "object", "properties": {"order_id": {"type": "string"}}}

    def __init__(self, answer: ToolOutput) -> None:
        self._answer = answer

    async def run(self, input: ToolInput) -> ToolOutput:
        return self._answer

    def format_output(self, data) -> str:
        return f"Order {data['order_id']} cancelled."


def _orchestrator(tmp_path: Path, tool: Tool) -> tuple[Orchestrator, AgentRunStore]:
    runs = AgentRunStore(tmp_path / "tasks.db")
    stream = EventStreamManager(run_store=runs)
    approvals = ApprovalStore()
    ledger = MagicMock()
    ledger.write = AsyncMock()
    registry = ToolRegistry()
    registry.register(bind_approvals(tool, ApprovalMode.YOLO))
    orchestrator = Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        interaction=UserInteraction(approvals, notifier=MagicMock(), stream_manager=stream),
        stream_manager=stream,
        approval_store=approvals,
        tool_registry=registry,
        tool_input_factory=ToolInput,
        agent_run_store=runs,
    )
    return orchestrator, runs


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        task_id="t1",
        agents=[],
        parallel_groups=[],
        dependencies={},
        mode=ExecutionMode.SINGLE_TOOL,
        direct_tool="cancel_order",
        direct_tool_params={"order_id": "4471"},
    )


async def _record(runs: AgentRunStore) -> tuple[list, list[dict]]:
    await drain()
    task_runs = await runs.list_for_task("t1")
    events = [event for run in task_runs for event in await runs.list_events(run.run_id)]
    return task_runs, events


async def test_a_direct_call_is_a_run_with_its_call_and_answer(tmp_path: Path) -> None:
    tool = _CancelOrder(ToolOutput(success=True, data={"order_id": "4471"}))
    orchestrator, runs = _orchestrator(tmp_path, tool)

    await orchestrator._execute_single_tool("t1", "pls cancel order 4471", _plan(), workspace=str(tmp_path))

    [run], events = await _record(runs)
    assert (run.agent, run.status, run.output) == ("tool_executor", "completed", "Order 4471 cancelled.")
    by_name = {e["event"]: e["data"] for e in events}  # written in the background, in either order
    assert sorted(by_name) == ["tool_called", "tool_result"]
    assert by_name["tool_called"]["params"] == {"order_id": "4471"}
    assert by_name["tool_result"]["formatted"] == "Order 4471 cancelled."
    [answer] = [c.args[0] for c in orchestrator._ledger.write.await_args_list if c.args[0].action == "agent_completed"]
    assert answer.tools_used == ["cancel_order"]
    assert answer.run_id == run.run_id


async def test_a_direct_call_the_agent_takes_over_is_recorded_and_the_agent_told_what_was_sent(
    tmp_path: Path,
) -> None:
    tool = _CancelOrder(ToolOutput(success=False, error="No order 4471.", failure_kind="error"))
    orchestrator, runs = _orchestrator(tmp_path, tool)
    told: list[str] = []

    async def fall_back(task_id, prompt, workspace, context, edit_scope, attempt) -> None:
        told.append(attempt)

    orchestrator._fall_back_to_agent = fall_back  # type: ignore[method-assign]

    await orchestrator._execute_single_tool("t1", "pls cancel order 4471", _plan(), workspace=str(tmp_path))

    [run], events = await _record(runs)
    assert (run.agent, run.status, run.error) == ("tool_executor", "failed", "No order 4471.")
    assert sorted(e["event"] for e in events) == ["tool_called", "tool_result"]
    assert told == ['`cancel_order` was called with {"order_id": "4471"} and answered: No order 4471.']


async def test_a_direct_change_you_refused_ends_the_task_blocked(tmp_path: Path) -> None:
    refused = ToolOutput(success=False, error="Order cancellation rejected by user.", failure_kind="refused")
    orchestrator, runs = _orchestrator(tmp_path, _CancelOrder(refused))

    await orchestrator._execute_single_tool("t1", "pls cancel order 4471", _plan(), workspace=str(tmp_path))

    writes = [c.args[0] for c in orchestrator._ledger.write.await_args_list]
    [ending] = [entry for entry in writes if entry.action == "task_completed"]
    assert ending.error_type == "blocked"
    [run], _ = await _record(runs)
    assert run.status == "completed"


def test_a_file_written_with_bash_is_evidence_for_writing_it() -> None:
    assert verify_claims("Added the line to journal.txt.", ["bash"]) == []
    assert verify_claims("Added the line to journal.txt.", ["read_file"]) != []
