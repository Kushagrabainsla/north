from __future__ import annotations

import sqlite3

from agents.models import AgentPayload, AgentResult
from orchestrator.agent_runs import AgentRunStore
from orchestrator.stream import EventStreamManager
from utils.execution_context import ExecutionIdentity, bind_execution


async def test_run_store_preserves_hierarchy_skills_outcome_and_events(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "tasks.db")
    payload = AgentPayload(
        task_id="task-1",
        run_id="child-run",
        parent_run_id="parent-run",
        attempt=1,
        prompt="inspect auth",
        workspace="/workspace",
        delegation_depth=1,
    )
    await store.start(payload, "researcher")
    await store.set_skills(
        payload.run_id,
        [{"name": "research", "version": "2.0.0", "source": "learned"}],
    )

    stream = EventStreamManager(run_store=store)
    with bind_execution(ExecutionIdentity(payload.run_id, payload.parent_run_id, payload.attempt)):
        await stream.emit(payload.task_id, "tool_called", {"tool": "read_file", "params": {"path": "a.py"}})

    await store.merge_provider_state(
        payload.run_id,
        {"provider": "openai_codex", "response_id": "resp-1", "item_ids": ["item-1"]},
    )
    await store.complete(
        payload.run_id,
        AgentResult(
            output="done",
            summary="inspected",
            run_id=payload.run_id,
            parent_run_id=payload.parent_run_id,
            attempt=1,
            duration_ms=25,
            tokens_in=10,
            tokens_out=4,
            models_used=["codex-model"],
        ),
    )

    run = await store.get(payload.run_id)
    assert run is not None
    assert run.parent_run_id == "parent-run"
    assert run.attempt == 1
    assert run.status == "completed"
    assert run.skills == ({"name": "research", "version": "2.0.0", "source": "learned"},)
    assert run.provider_state["openai_codex"][0]["response_id"] == "resp-1"

    events = await store.list_events(payload.run_id)
    assert events[0]["event"] == "tool_called"
    assert events[0]["data"]["parent_run_id"] == "parent-run"

    with sqlite3.connect(tmp_path / "tasks.db") as conn:
        usage = conn.execute(
            "SELECT outcome, tokens_in, tokens_out FROM skill_usage WHERE run_id=?", (payload.run_id,)
        ).fetchone()
    assert usage == ("completed", 10, 4)


async def test_failed_run_marks_skill_usage_failed(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "tasks.db")
    payload = AgentPayload(task_id="task-1", run_id="failed-run", prompt="fail")
    await store.start(payload, "coder")
    await store.set_skills(
        payload.run_id,
        [{"name": "debug", "version": "1.0.0", "source": "builtin"}],
    )
    await store.finish_with_error(payload.run_id, "failed", "boom")

    run = await store.get(payload.run_id)
    assert run is not None and run.status == "failed" and run.error == "boom"
    with sqlite3.connect(tmp_path / "tasks.db") as conn:
        outcome = conn.execute("SELECT outcome FROM skill_usage WHERE run_id=?", (payload.run_id,)).fetchone()
    assert outcome == ("failed",)
