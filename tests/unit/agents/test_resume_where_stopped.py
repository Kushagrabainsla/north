"""A paused or interrupted task resumes where it stopped (CODING_STYLE §15.5).

Issue #76, M19: Resume started the agent over (a second `agent_started`) and redid
the glob and every read it had already made (task_d8af60e8e247).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.models import AgentConfig, AgentDependencies, AgentPayload
from inference.models import ToolCall, ToolCallResponse
from memory import FileContextStore
from orchestrator.agent_runs import AgentRunStore
from tests.conftest import MockInferenceRouter
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parents[3] / "agents"
PROMPT = "summarise my two notes"


class _ReadsNotes(Tool):
    name = "read_note"
    description = "Reads one of the user's notes."
    parameters_schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def __init__(self) -> None:
        self.read: list[str] = []

    async def run(self, input: ToolInput) -> ToolOutput:
        self.read.append(input.params["name"])
        return ToolOutput(success=True, data={"text": f"note {input.params['name']}"})


class _Model(MockInferenceRouter):
    """Reads note 1, then note 2, then answers, going by the conversation it is sent.

    While `stalled`, the call after the first read never returns: that is where the task is paused.
    """

    def __init__(self) -> None:
        self.stalled = True
        self.reached_pause = asyncio.Event()

    async def complete_with_tools(self, request, token_callback=None):
        results = [m for m in request.messages if m.get("role") == "tool"]
        if len(results) == 1 and self.stalled:
            self.reached_pause.set()
            await asyncio.Event().wait()
        if len(results) < 2:
            call = ToolCall(name="read_note", call_id=f"c{len(results)}", params={"name": str(len(results) + 1)})
            return ToolCallResponse(type="tool_calls", calls=[call], model_used="mock")
        return ToolCallResponse(type="message", content="Both notes are about north.", calls=[], model_used="mock")


def _agent(tmp_path: Path, model: _Model, runs: AgentRunStore, notes: _ReadsNotes):
    registry = ToolRegistry(auto_register=False)
    registry.register(notes)
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=model,
        tool_registry=registry,
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    deps.agent_run_store = runs
    from agents.general.agent import GeneralAgent

    return GeneralAgent(AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml"), deps)


async def _paused_after_one_read(tmp_path: Path) -> tuple[_Model, AgentRunStore, _ReadsNotes]:
    model, runs, notes = _Model(), AgentRunStore(tmp_path / "tasks.db"), _ReadsNotes()
    running = asyncio.create_task(_agent(tmp_path, model, runs, notes).run(AgentPayload(task_id="t1", prompt=PROMPT)))
    await asyncio.wait_for(model.reached_pause.wait(), 5)
    running.cancel()  # Pause cancels the task's pipeline
    await asyncio.gather(running, return_exceptions=True)
    return model, runs, notes


async def test_a_resumed_run_carries_on_from_its_last_step_and_redoes_nothing(tmp_path: Path) -> None:
    model, runs, notes = await _paused_after_one_read(tmp_path)
    model.stalled = False

    result = await asyncio.wait_for(
        _agent(tmp_path, model, runs, notes).run(AgentPayload(task_id="t1", prompt=PROMPT)), 5
    )

    assert notes.read == ["1", "2"]  # note 1 was read once, before the pause
    assert result.output == "Both notes are about north."
    assert "read_note" in (result.successful_tools or [])  # the answer is checked against both reads
    [run] = await runs.list_for_task("t1")  # the same run, carried on
    assert run.status == "completed" and run.run_id == result.run_id


async def test_a_new_task_or_another_step_starts_afresh(tmp_path: Path) -> None:
    model, runs, notes = await _paused_after_one_read(tmp_path)
    model.stalled = False

    await asyncio.wait_for(_agent(tmp_path, model, runs, notes).run(AgentPayload(task_id="t2", prompt=PROMPT)), 5)
    await asyncio.wait_for(
        _agent(tmp_path, model, runs, notes).run(AgentPayload(task_id="t1", prompt="something else")), 5
    )

    assert notes.read == ["1", "1", "2", "1", "2"]


async def test_only_a_run_stopped_from_outside_is_continued(tmp_path: Path) -> None:
    runs = AgentRunStore(tmp_path / "tasks.db")
    for run_id, status in (("paused", "cancelled"), ("cut_off", "interrupted"), ("broke", "failed")):
        payload = AgentPayload(task_id=f"t-{run_id}", prompt=PROMPT, run_id=run_id)
        await runs.start(payload, "general")
        await runs.save_checkpoint(run_id, {"messages": [], "messages_read": 0, "step": 1, "evidence": {}})
        await runs.finish_with_error(run_id, status, "stopped")
    await runs.start(AgentPayload(task_id="t-unsaved", prompt=PROMPT, run_id="unsaved"), "general")
    await runs.finish_with_error("unsaved", "cancelled", "stopped before its first step")

    assert await runs.continuable_run("t-paused", "general", PROMPT) == "paused"
    assert await runs.continuable_run("t-cut_off", "general", PROMPT) == "cut_off"
    assert await runs.continuable_run("t-broke", "general", PROMPT) is None
    assert await runs.continuable_run("t-unsaved", "general", PROMPT) is None
