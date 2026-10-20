"""A correction pass continues a finished run: it repeats nothing, and asks the user nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.agentic_llm_agent import _tool_schemas
from agents.models import AgentPayload, AgentResult
from orchestrator.result_audit import ResultAuditor

# The real task: a correction pass after it ran the whole task again, validated again
# and asked the user again to confirm what was done. A gap is made here by an answer
# claiming a skill was created when only validate and activate ran.
_TASK = (
    "Re-activate the research-paper-briefing skill, which I edited. Use create_skill: validate it, "
    "then ask me to confirm and activate it."
)


class _Journal:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def emit(self, task_id: str, event: str, data: dict) -> None:
        return None

    async def record(self, task_id: str, action: str, **kwargs) -> None:
        self.recorded.append(action)

    async def stream_note(self, task_id: str, note: str) -> None:
        return None


class _Agent:
    name = "general"
    domain = "general"

    def __init__(self) -> None:
        self.payloads: list[AgentPayload] = []

    async def run(self, payload: AgentPayload) -> AgentResult:
        self.payloads.append(payload)
        return AgentResult(
            output="No create or update was needed: the skill was edited beforehand. It is active.",
            summary="active",
            successful_tools=[],
        )


def _finished_run() -> AgentResult:
    return AgentResult(
        output="I created the research-paper-briefing skill and it is active again.",
        summary="active",
        run_id="run-1",
        successful_tools=["create_skill", "request_approval", "create_skill"],
        data={"evidence_actions": {"create_skill:validate": 1, "request_approval": 1, "create_skill:activate": 1}},
    )


@pytest.mark.asyncio
async def test_the_correction_pass_is_told_what_was_done_and_cannot_ask_again() -> None:
    agent = _Agent()
    auditor = ResultAuditor(journal=_Journal())

    await auditor.verify_claims("t1", agent, _finished_run(), AgentPayload(task_id="t1", prompt=_TASK))

    (repair,) = agent.payloads
    assert repair.allow_questions is False
    assert repair.attempt == 1 and repair.parent_run_id == "run-1"
    assert "Your previous answer was:\nI created the research-paper-briefing skill" in repair.prompt
    assert (
        "Already done in this task, by tools that succeeded: "
        "create_skill:activate, create_skill:validate, request_approval. Do not repeat any of it." in repair.prompt
    )


def test_a_run_that_may_not_ask_is_not_offered_the_question_tools() -> None:
    names = lambda schemas: {s.get("function", s).get("name") for s in schemas}  # noqa: E731

    assert {"ask_user", "request_approval"} <= names(_tool_schemas({}, allow_delegation=False))
    assert not {"ask_user", "request_approval"} & names(
        _tool_schemas({}, allow_delegation=False, allow_questions=False)
    )


@pytest.mark.asyncio
async def test_a_run_that_may_not_ask_is_refused_a_question_and_no_card_is_made() -> None:
    from agents.agentic_llm_agent import AgenticLLMAgent

    agent = AgenticLLMAgent.__new__(AgenticLLMAgent)
    made: list[str] = []

    async def no_card(*args, **kwargs):
        made.append("card")

    agent._ask_user = no_card
    agent._request_approval_card = no_card
    call = SimpleNamespace(name="ask_user", params={"question": "Activate it now?"})
    payload = AgentPayload(task_id="t1", prompt=_TASK, allow_questions=False)

    _, result, success, _ = await agent._execute_call(call, payload, {})

    assert success is False and "may not ask the user" in result
    assert made == []
