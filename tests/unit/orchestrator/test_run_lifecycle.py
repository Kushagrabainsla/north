"""A run's recorded state is what happened to it.

In the issue #76 test: a mode change north made from chat reported success while
the running server kept the old mode until a restart (finding A4); cancelled
tasks left their flow test runs "running now" (M16); a restart left the run it
cut off "running" with no end time (M19); and a flow step handed a literal
``${inputs.journal_path}`` did nothing, yet the run was "completed" (M15).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agents.models import AgentResult
from config.approval_mode import ApprovalMode
from config.runtime import clear_runtime, set_runtime
from config.strategy import NorthSettings
from flows import FlowRunStore
from orchestrator.agent_runs import AgentRunStore
from orchestrator.app import _close_runs_cut_off
from tests.conftest import approving_store, bind_approvals
from tests.unit.flows.test_runner import FakeAgent, FakeAgents, _skills, _users_flow
from tests.unit.orchestrator.test_orchestrator_security import _orchestrator
from tools.models import ToolInput
from tools.specialized.north_config import NorthConfigTool
from tools.universal.flow_runner import FlowRunner
from utils.time import utcnow


async def test_a_mode_change_made_in_chat_is_the_mode_the_server_runs_in(tmp_path: Path) -> None:
    live = NorthSettings(tmp_path / "settings.json")
    set_runtime(SimpleNamespace(north_settings=live))
    try:
        tool = bind_approvals(NorthConfigTool(), ApprovalMode.ASK, store=approving_store())
        out = await tool.execute(ToolInput(params={"action": "autonomy", "value": "autonomous"}))
    finally:
        clear_runtime()

    assert out.success, out.error
    assert live.autonomy is ApprovalMode.AUTONOMOUS
    assert NorthSettings(tmp_path / "settings.json").autonomy is ApprovalMode.AUTONOMOUS  # and it is saved


async def test_cancelling_a_task_ends_the_flow_run_it_started(tmp_path: Path) -> None:
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_users_flow(tmp_path, _ONE_STEP), FakeAgents(FakeAgent()), _skills(tmp_path), store)
    store.create(run_id="r1", flow_name="demo", task_id="t1", trigger="test", test_mode=True)
    orchestrator = _orchestrator()
    orchestrator.when_cancelled(runner.cancel_running)
    orchestrator._active_tasks["t1"] = asyncio.create_task(asyncio.sleep(60))

    assert await orchestrator.cancel_task("t1")

    assert store.get("r1").status == "cancelled"


async def test_a_restart_closes_the_runs_the_last_stop_cut_off(tmp_path: Path) -> None:
    runs = AgentRunStore(tmp_path / "tasks.db")
    for run_id, agent in (("cut-off", "general"), ("coding", "coding:claude")):
        await runs.start(
            SimpleNamespace(
                run_id=run_id, task_id="t1", parent_run_id=None, attempt=0, prompt="p", workspace="", delegation_depth=0
            ),
            agent,
        )
    started = utcnow()
    await runs.start(
        SimpleNamespace(
            run_id="new", task_id="t1", parent_run_id=None, attempt=1, prompt="p", workspace="", delegation_depth=0
        ),
        "general",
    )

    assert await _close_runs_cut_off(runs, before=started) == 1

    statuses = {run.run_id: run.status for run in await runs.list_unfinished("")}
    assert "cut-off" not in statuses
    assert statuses == {"coding": "running", "new": "running"}  # the coding run's recorder checks its process


_ONE_STEP = """name: demo
description: Demo flow
steps:
  - name: write
    skill: review-item
    instructions: Append "day done" to ${inputs.journal_path}.
    approval: never
"""


async def test_a_step_missing_an_input_its_instructions_name_stops_before_it_runs(tmp_path: Path) -> None:
    agent = FakeAgent()
    runner = FlowRunner(
        _users_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), FlowRunStore(tmp_path / "r.db")
    )

    run = await runner.run("demo", trigger="test", test_mode=True)

    assert run.status == "paused"
    assert "${inputs.journal_path}" in run.error
    assert agent.payloads == []


async def test_a_steps_instructions_carry_the_inputs_given(tmp_path: Path) -> None:
    agent = FakeAgent()
    runner = FlowRunner(
        _users_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), FlowRunStore(tmp_path / "r.db")
    )

    await runner.run("demo", inputs={"journal_path": "/home/sam/journal.txt"}, trigger="test", test_mode=True)

    assert 'Append "day done" to /home/sam/journal.txt.' in agent.payloads[0].prompt


class _Blocked(FakeAgent):
    async def run(self, payload):
        self.payloads.append(payload)
        return AgentResult(
            output='{"status": "blocked"}',
            summary="The write to journal.txt was refused.",
            data={"outcome_status": "blocked"},
            tools_used=["bash"],
        )


async def test_a_step_whose_agent_was_blocked_does_not_complete_the_run(tmp_path: Path) -> None:
    runner = FlowRunner(
        _users_flow(tmp_path, _ONE_STEP), FakeAgents(_Blocked()), _skills(tmp_path), FlowRunStore(tmp_path / "r.db")
    )

    run = await runner.run("demo", inputs={"journal_path": "/j.txt"}, trigger="test", test_mode=True)

    assert run.status == "paused"
    assert "could not finish" in run.error
