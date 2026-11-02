"""An answer north gives for you is told as north's, and a mode says when it asks.

In the issue #76 test, yolo answered "How would you like to proceed?" with its
first option, "I'll delete it myself", and the agent, given only the answer, told
the user "you've said you'll delete it yourself"; another run said "You approved
the deletion" of a card yolo approved (A15). A model-written "Delete
old_safe.log ... Approve to delete it" card was approved and nothing was deleted:
no tool of the agent's could delete (A14). Autonomous, "Never asks you", raised a
card while north.log called the memory decider's abstention an "unreadable
reply" (A16).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from agents.models import AgentPayload
from agents.schemas import REQUEST_APPROVAL_SCHEMA
from approval.models import Card, CardType
from approval.store import ApprovalStore
from config.approval_mode import YES_ANSWER, ApprovalMode
from inference.models import ToolCall
from tests.conftest import StubDecider, approval_policy
from tests.unit.agents.test_autonomous_ask_user import _agent, _ask
from tests.unit.approval.test_decider import _decider, _push


def _question(*options: str) -> Card:
    return Card.new(type=CardType.QUESTION, agent="general", title="Q", message="How?", options=list(options))


async def test_yolo_answers_a_question_with_no_yes_option_by_leaving_it_to_the_agent() -> None:
    answer = await approval_policy(ApprovalMode.YOLO).answer(
        _question("I'll delete it myself", "Create a delete-file tool", "Overwrite it with empty content")
    )

    assert answer is not None and answer.chosen_option == YES_ANSWER


def test_the_yes_option_is_found_wherever_it_is_listed() -> None:
    from config.approval_mode import affirmative_option

    assert affirmative_option(["No, keep it", "Yes, delete it"]) == "Yes, delete it"
    assert affirmative_option(["Keep on branch", "Apply"]) == "Apply"
    assert affirmative_option(["Postgres", "SQLite"]) is None


async def test_the_agent_is_told_yolo_answered_its_question_not_the_user(tmp_path: Path) -> None:
    agent = _agent(tmp_path, ApprovalMode.YOLO, StubDecider(None), ApprovalStore())

    out = json.loads(await _ask(agent, "Delete it?", ("Yes", "No")))

    assert out["answer"] == "Yes"
    assert "yolo" in out["answered_by"] and "the user has not seen" in out["answered_by"]


async def test_the_agent_is_told_yolo_approved_its_card_not_the_user(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.YOLO, StubDecider(None), store)
    call = ToolCall(name="request_approval", call_id="c1", params={"title": "Delete old_yolo.log", "message": "?"})

    _, result, ok, _ = await agent._execute_call(call, AgentPayload(task_id="t1", prompt="p"), {})

    assert ok and json.loads(result)["decision"] == "approved"
    assert "yolo" in json.loads(result)["decided_by"]


async def test_the_agent_is_told_when_the_user_answered(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)
    asking = asyncio.create_task(_ask(agent))
    await asyncio.sleep(0)
    [card] = store.pending()
    store.resolve(card.id, "answered", chosen_option="SQLite", decided_by="you")

    assert json.loads(await asking)["answered_by"] == "the user"


def test_a_card_approval_runs_nothing_and_no_card_is_raised_for_what_no_tool_can_do() -> None:
    description = REQUEST_APPROVAL_SCHEMA["function"]["description"]

    assert "runs nothing" in description
    assert "no tool" in description


def test_autonomous_says_when_it_asks() -> None:
    text = ApprovalMode.AUTONOMOUS.description

    assert "Never asks" not in text
    assert "outside the workspace" in text


async def test_an_abstention_is_logged_as_one_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    decider, _ = _decider({"decision": "abstain", "reason": "nothing you said covers pushing to origin"})

    with caplog.at_level(logging.INFO, logger="approval.decider"):
        assert await decider.rule(_push()) is None

    assert "abstained" in caplog.text and "nothing you said covers pushing to origin" in caplog.text
    assert "unreadable" not in caplog.text
