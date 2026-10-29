"""A message you send while a task works reaches the agents working on it (CODING_STYLE §15.4).

A second message started a parallel task that knew nothing of the first, and
`/steer` said "steering sent" while nothing read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.inbox import TaskInbox
from agents.models import AgentPayload
from inference.models import ToolCallResponse
from tests.conftest import MockInferenceRouter
from tests.unit.agents.test_react_loop import _load_agent

TASK = "t1"


def _texts(request) -> list[str]:
    return [str(message.get("content") or "") for message in request.messages if message.get("role") == "user"]


class _Router(MockInferenceRouter):
    """Answers every call; before answering the *send_on*-th call, the user sends a message."""

    def __init__(self, inbox: TaskInbox, send_on: int = 0) -> None:
        self.inbox = inbox
        self.send_on = send_on
        self.requests: list = []

    async def complete_with_tools(self, request, token_callback=None):
        self.requests.append(request)
        if len(self.requests) == self.send_on:
            self.inbox.post(TASK, "use the 2025 numbers instead")
        return ToolCallResponse(type="message", content=f"answer {len(self.requests)}", calls=[], model_used="mock")


def _agent(tmp_path: Path, router: _Router, inbox: TaskInbox):
    agent = _load_agent("general", tmp_path, router)
    agent._deps.task_inbox = inbox
    return agent


@pytest.mark.asyncio
async def test_a_message_sent_before_its_next_step_is_in_that_step(tmp_path: Path) -> None:
    inbox = TaskInbox()
    inbox.open(TASK)
    inbox.post(TASK, "use the 2025 numbers instead")
    router = _Router(inbox)

    result = await _agent(tmp_path, router, inbox).run(AgentPayload(task_id=TASK, prompt="Summarise sales."))

    assert result.output == "answer 1"
    assert any("use the 2025 numbers instead" in text for text in _texts(router.requests[0]))


@pytest.mark.asyncio
async def test_a_message_sent_while_it_answers_makes_it_answer_again_with_it(tmp_path: Path) -> None:
    inbox = TaskInbox()
    inbox.open(TASK)
    router = _Router(inbox, send_on=1)

    result = await _agent(tmp_path, router, inbox).run(AgentPayload(task_id=TASK, prompt="Summarise sales."))

    assert result.output == "answer 2"
    assert len(router.requests) == 2
    assert any("use the 2025 numbers instead" in text for text in _texts(router.requests[1]))
    assert inbox.take_unread(TASK) == []


@pytest.mark.asyncio
async def test_a_delegated_agent_leaves_your_messages_to_its_caller(tmp_path: Path) -> None:
    inbox = TaskInbox()
    inbox.open(TASK)
    inbox.post(TASK, "use the 2025 numbers instead")
    router = _Router(inbox)

    await _agent(tmp_path, router, inbox).run(AgentPayload(task_id=TASK, prompt="Look it up.", delegation_depth=1))

    assert not any("2025" in text for text in _texts(router.requests[0]))
    assert inbox.take_unread(TASK) == ["use the 2025 numbers instead"]


def test_every_agent_reads_every_message_and_unread_ones_are_handed_over_once() -> None:
    inbox = TaskInbox()
    inbox.open(TASK)
    inbox.post(TASK, "first")

    assert inbox.read(TASK, 0) == ["first"]
    assert inbox.read(TASK, 0) == ["first"]  # a second agent reads from its own position
    inbox.post(TASK, "second")
    assert inbox.take_unread(TASK) == ["second"]
    assert inbox.take_unread(TASK) == []


def test_a_task_that_starts_over_opens_with_earlier_messages_as_known() -> None:
    inbox = TaskInbox()
    inbox.open(TASK, ["sent before the restart"])

    assert inbox.read(TASK, 0) == ["sent before the restart"]
    assert inbox.take_unread(TASK) == []


def test_a_finished_task_takes_no_messages() -> None:
    inbox = TaskInbox()

    assert inbox.post(TASK, "too late") is False
    inbox.open(TASK)
    inbox.close(TASK)
    assert inbox.post(TASK, "too late") is False
