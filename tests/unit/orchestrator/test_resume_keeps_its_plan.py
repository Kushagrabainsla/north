"""A task that resumes carries on with its plan, and does not make again a call it already made (§15.5).

Issue #76, M19: Resume ran the whole task again, planning included, and redid
every step it had taken before the pause.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agents.models import AgentPayload
from orchestrator.agent_runs import AgentRunStore
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification, TaskRequest
from orchestrator.orchestrator import DIRECT_TOOL_AGENT
from orchestrator.running_tasks import RunningTaskStore
from tests.unit.orchestrator.test_orchestrator_security import _orchestrator

PLAN = ExecutionPlan(
    task_id="t1",
    agents=["general"],
    parallel_groups=[["general"]],
    dependencies={},
    mode=ExecutionMode.SINGLE_TOOL,
    direct_tool="kasa",
    direct_tool_params={"action": "off", "device": "Kitchen light"},
)
CLASSIFIED = IntentClassification(is_consequential=False, domain="general", reasoning="one device call")


async def test_a_task_plans_once_and_resumes_with_that_plan(tmp_path: Path) -> None:
    orchestrator = _orchestrator()
    orchestrator._running_task_store = RunningTaskStore(tmp_path / "tasks.db")
    await orchestrator._running_task_store.mark_running("t1", TaskRequest(prompt="switch off the kitchen light"))
    orchestrator._execution_planner.plan_all = AsyncMock(return_value=(CLASSIFIED, PLAN))
    orchestrator._task_context_store = MagicMock(initialize_task=AsyncMock())

    first = await orchestrator._stage_plan("t1", "switch off the kitchen light")
    resumed = await orchestrator._stage_plan("t1", "switch off the kitchen light")

    assert orchestrator._execution_planner.plan_all.await_count == 1
    assert first == resumed == (CLASSIFIED, PLAN)


async def test_a_direct_call_the_agent_took_over_is_not_made_again(tmp_path: Path) -> None:
    orchestrator = _orchestrator()
    orchestrator._agent_run_store = runs = AgentRunStore(tmp_path / "runs.db")
    earlier = AgentPayload(task_id="t1", prompt="switch off the kitchen light")
    await runs.start(earlier, DIRECT_TOOL_AGENT)
    await runs.finish_with_error(earlier.run_id, "failed", "No Kasa devices found.")
    orchestrator._tool_registry = MagicMock(get=MagicMock(side_effect=AssertionError("called again")))
    orchestrator._fall_back_to_agent = AsyncMock()

    await orchestrator._execute_single_tool("t1", "switch off the kitchen light", PLAN, str(tmp_path))

    told = orchestrator._fall_back_to_agent.await_args.args[-1]
    assert told.startswith('`kasa` was called with {"action": "off", "device": "Kitchen light"}')
    assert told.endswith("and answered: No Kasa devices found.")
