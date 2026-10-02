"""Question: does a coding run survive a daemon restart under north's real recovery rules?"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.models import AgentPayload
from orchestrator.agent_runs import AgentRunStore
from orchestrator.models import TaskRequest
from orchestrator.reconcile import recover_interrupted_tasks
from orchestrator.running_tasks import RunningTaskStore

from .prototypes import record


async def _recover(tmp_path, *, side_effect: bool):
    store = RunningTaskStore(tmp_path / "running.db")
    await store.mark_running("t-run", TaskRequest(prompt="fix the bug", workspace="/w"))
    if side_effect:
        await store.mark_side_effect("t-run")
    deps = SimpleNamespace(running_task_store=store, ledger=SimpleNamespace(write=AsyncMock()))
    orchestrator = SimpleNamespace(active_task_ids=frozenset(), resume_task=AsyncMock(return_value=True))
    await recover_interrupted_tasks(deps, orchestrator, max_age_seconds=3600)
    return orchestrator, deps


async def test_a_task_killed_mid_coding_run_is_resumed_not_failed(tmp_path) -> None:
    """The tool has not returned, so no mutating call has 'succeeded' yet: no side effect is recorded."""
    orchestrator, _ = await _recover(tmp_path, side_effect=False)

    orchestrator.resume_task.assert_awaited_once()


async def test_a_task_killed_after_a_finished_mutating_call_is_failed_with_a_note(tmp_path) -> None:
    """After apply-back the task has acted; north will not re-run it. That is the right rule for us too."""
    orchestrator, deps = await _recover(tmp_path, side_effect=True)

    orchestrator.resume_task.assert_not_awaited()
    entry = deps.ledger.write.await_args.args[0]
    assert "not auto-resumed" in entry.output
    record("R1_recovery_rule", {"mid_run": "resumed", "after_side_effect": "failed with note"})


async def test_a_resumed_task_can_find_its_live_coding_run_by_task_id(tmp_path) -> None:
    """The lookup the tool needs so a re-planned task resumes the session instead of starting a second one."""
    store = AgentRunStore(tmp_path / "runs.db")
    first = AgentPayload(task_id="t-run", prompt="fix the bug", workspace="/w", delegation_depth=1)
    await store.start(first, "coding:claude")
    await store.merge_provider_state(first.run_id, {"provider": "claude_code", "session_id": "uuid-1"})
    other = AgentPayload(task_id="t-run", prompt="summarize", workspace="/w")
    await store.start(other, "general")

    live = [r for r in await store.list_for_task("t-run") if r.agent.startswith("coding:") and r.status == "running"]

    assert [r.provider_state["claude_code"][-1]["session_id"] for r in live] == ["uuid-1"]


@pytest.mark.xfail(strict=True, reason="a run is only 'running' or terminal; nothing marks 'resumable after a crash'")
async def test_a_dead_run_can_be_marked_resumable(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.db")
    assert hasattr(store, "mark_interrupted")
