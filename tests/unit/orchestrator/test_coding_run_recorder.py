"""A coding run in the real run store: visible on the dashboard, resumable after a crash."""

from __future__ import annotations

import pytest

from coding_agents import FailureKind, RunOutcome, RunStart
from orchestrator.agent_runs import AgentRunStore, RunStatus
from orchestrator.coding_run_recorder import AgentRunRecorder
from utils.execution_context import ExecutionIdentity, bind_execution

AGENT = "coding:claude"


@pytest.fixture
def store(tmp_path) -> AgentRunStore:
    return AgentRunStore(tmp_path / "tasks.db")


def _start(run_id: str = "run-1", task_id: str = "t1", agent: str = AGENT) -> RunStart:
    return RunStart(run_id=run_id, task_id=task_id, agent=agent, prompt="plan it", workspace="/w")


async def _begin(recorder: AgentRunRecorder, **state) -> None:
    await recorder.start(_start())
    await recorder.remember("run-1", {"provider": "claude_code", "session_id": "sess-1", "mode": "plan", **state})


async def test_a_run_appears_in_the_store_as_a_running_coding_agent(store) -> None:
    recorder = AgentRunRecorder(store)

    await _begin(recorder, pid=4242)
    await recorder.record("run-1", "t1", "tool_use", {"tool": "Read", "summary": "calc.py"})

    (run,) = await store.list_for_task("t1")
    assert (run.agent, run.status, run.workspace) == (AGENT, "running", "/w")
    assert run.provider_state["claude_code"][-1]["session_id"] == "sess-1"
    assert [event["event"] for event in await store.list_events("run-1")] == ["tool_use"]


async def test_it_is_a_child_of_the_agent_run_that_asked_for_it(store) -> None:
    recorder = AgentRunRecorder(store)

    with bind_execution(ExecutionIdentity(run_id="parent-run")):
        await recorder.start(_start())

    (run,) = await store.list_for_task("t1")
    assert (run.parent_run_id, run.delegation_depth) == ("parent-run", 1)


async def test_a_finished_plan_completes_the_run_with_its_answer_and_cost(store) -> None:
    recorder = AgentRunRecorder(store)
    await _begin(recorder)

    await recorder.finish("run-1", RunOutcome(ok=True, text="the plan", session_id="sess-1", cost_usd=0.12))

    run = await store.get("run-1")
    assert (run.status, run.output, run.cost_usd) == ("completed", "the plan", 0.12)


async def test_the_tokens_an_agent_used_reach_the_run_record(store) -> None:
    recorder = AgentRunRecorder(store)
    await _begin(recorder)

    await recorder.finish(
        "run-1", RunOutcome(ok=True, text="done", session_id="sess-1", tokens_in=58_262, tokens_out=338)
    )

    run = await store.get("run-1")
    assert (run.tokens_in, run.tokens_out) == (58_262, 338)


@pytest.mark.parametrize(
    ("failure", "status"),
    [(FailureKind.ERROR, "failed"), (FailureKind.RESOURCE, "failed"), (FailureKind.CANCELLED, "cancelled")],
)
async def test_a_run_that_did_not_finish_says_how_it_ended(store, failure, status) -> None:
    recorder = AgentRunRecorder(store)
    await _begin(recorder)

    await recorder.finish("run-1", RunOutcome(False, "", "sess-1", failure=failure, error="why"))

    run = await store.get("run-1")
    assert (run.status, run.error) == (status, "why")


class TestFindingARunToResume:
    async def test_a_run_whose_process_died_is_found_with_its_session(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await _begin(recorder, pid=4242)

        live = await recorder.live_run("t1", AGENT, "plan")

        assert (live.run_id, live.session_id) == ("run-1", "sess-1")

    async def test_a_run_whose_process_is_still_alive_is_not_taken_over(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: True)
        await _begin(recorder, pid=4242)

        assert await recorder.live_run("t1", AGENT, "plan") is None

    async def test_an_interrupted_run_is_found_even_if_its_pid_was_reused(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: True)
        await _begin(recorder, pid=4242)
        await store.set_status("run-1", RunStatus.INTERRUPTED)

        assert (await recorder.live_run("t1", AGENT, "plan")).session_id == "sess-1"

    async def test_a_finished_run_is_not_resumed(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await _begin(recorder, pid=4242)
        await recorder.finish("run-1", RunOutcome(ok=True, text="done", session_id="sess-1"))

        assert await recorder.live_run("t1", AGENT, "plan") is None

    async def test_another_task_or_another_agent_is_not_resumed(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await _begin(recorder, pid=4242)

        assert await recorder.live_run("other-task", AGENT, "plan") is None
        assert await recorder.live_run("t1", "coding:codex", "plan") is None

    async def test_a_run_that_never_saved_a_session_cannot_be_resumed(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await recorder.start(_start())

        assert await recorder.live_run("t1", AGENT, "plan") is None

    async def test_a_plan_run_is_not_resumed_by_an_edit_call(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await _begin(recorder, pid=4242)

        assert await recorder.live_run("t1", AGENT, "edit") is None

    async def test_an_edit_run_comes_back_with_its_worktree(self, store) -> None:
        recorder = AgentRunRecorder(store, pid_alive=lambda pid: False)
        await recorder.start(_start())
        await recorder.remember(
            "run-1",
            {
                "provider": "claude_code",
                "session_id": "sess-1",
                "mode": "edit",
                "pid": 4242,
                "worktree": "/tmp/wt",
                "branch": "north/wt-x",
                "base_sha": "abc",
                "base": "/repo",
            },
        )

        live = await recorder.live_run("t1", AGENT, "edit")

        assert (live.worktree.path, live.worktree.branch, live.worktree.base_sha, live.worktree.base) == (
            "/tmp/wt",
            "north/wt-x",
            "abc",
            "/repo",
        )
