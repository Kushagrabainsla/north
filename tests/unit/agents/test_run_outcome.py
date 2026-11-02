"""How a run ended comes from what it did, not from the words of its answer.

The outcome badge was a regex over the answer (issue #76, finding M10): "Yes.
blocked" was badged BLOCKED, and a full answer ending "Do you want a fun fact?"
WAITING FOR USER, while a note that could not be saved, answered "I couldn't
save this", was RESPONSE READY. The Tasks page meanwhile ignored the outcome and
listed blocked tasks as completed, finished work (P11).
"""

from __future__ import annotations

from pathlib import Path

from agents.agentic_llm_agent import _RunTally
from agents.models import AgentPayload
from config.approval_mode import ApprovalMode
from inference.models import ToolCall, ToolCallResponse
from tests.conftest import MockInferenceRouter, bind_approvals
from tests.unit.agents.test_react_loop import _make_deps
from tests.unit.orchestrator.test_orchestrator_security import _orchestrator
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class _Scripted(MockInferenceRouter):
    """A model that answers with these turns, in order."""

    def __init__(self, *turns: ToolCallResponse) -> None:
        self._turns = list(turns)

    async def complete_with_tools(self, request, token_callback=None) -> ToolCallResponse:
        return self._turns.pop(0)


def _says(text: str) -> ToolCallResponse:
    return ToolCallResponse(type="message", content=text, model_used="m")


def _calls(name: str) -> ToolCallResponse:
    return ToolCallResponse(type="tool_calls", calls=[ToolCall(name=name, call_id=name, params={})], model_used="m")


class _SaveNote(Tool):
    name = "save_note"
    description = "Saves a note."
    is_mutating = True
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self, works: bool) -> None:
        self._works = works

    async def run(self, input: ToolInput) -> ToolOutput:
        if self._works:
            return ToolOutput(success=True, data={})
        return ToolOutput(success=False, error="That path is blocked: it holds north's own data.")


async def _outcome(tmp_path: Path, *turns: ToolCallResponse, tools: tuple[Tool, ...] = ()) -> str:
    from agents.general.agent import GeneralAgent
    from agents.models import AgentConfig
    from tests.unit.agents.test_react_loop import AGENTS_DIR

    deps = _make_deps(tmp_path, _Scripted(*turns))
    for tool in tools:
        deps.tool_registry.register(bind_approvals(tool, ApprovalMode.YOLO))
    agent = GeneralAgent(AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml"), deps)
    result = await agent.run(AgentPayload(task_id="t1", prompt="p", allowed_tools=[t.name for t in tools] or None))
    return result.data["outcome_status"]


async def test_an_answer_that_mentions_blocked_is_just_an_answer(tmp_path: Path) -> None:
    assert await _outcome(tmp_path, _says("Yes. blocked")) == "response_ready"


async def test_an_answer_ending_in_a_question_is_not_waiting(tmp_path: Path) -> None:
    assert await _outcome(tmp_path, _says("17 x 23 = 391. Do you want a fun fact?")) == "response_ready"


async def test_a_change_that_failed_is_blocked_whatever_the_answer_says(tmp_path: Path) -> None:
    outcome = await _outcome(
        tmp_path,
        _calls("save_note"),
        _says("I couldn't save this to a notes file, so here it is to copy."),
        tools=(_SaveNote(works=False),),
    )

    assert outcome == "blocked"


async def test_a_change_that_succeeded_after_a_failure_is_not_blocked(tmp_path: Path) -> None:
    deps_tools = (_SaveNote(works=True),)
    outcome = await _outcome(tmp_path, _calls("save_note"), _says("Saved."), tools=deps_tools)

    assert outcome == "response_ready"


def test_a_card_left_for_you_is_waiting_and_a_later_change_does_not_hide_a_failed_one() -> None:
    tally = _RunTally()
    tally.note_outcome("request_approval", {"wait": False}, mutating=False, success=True)
    assert tally.outcome() == "waiting_for_user"

    tally.note_outcome("save_note", {}, mutating=True, success=False)
    assert tally.outcome() == "blocked"
    tally.note_outcome("save_note", {}, mutating=True, success=True)
    assert tally.outcome() == "waiting_for_user"


async def test_a_task_whose_work_was_blocked_ends_marked_blocked() -> None:
    orchestrator = _orchestrator()
    orchestrator._blocked_tasks.add("t1")

    await orchestrator._finish_task("t1", skip_extraction=True)

    writes = orchestrator._ledger.write.await_args_list
    [ending] = [c.args[0] for c in writes if c.args[0].action == "task_completed"]
    assert ending.error_type == "blocked"
