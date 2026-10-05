"""A coding agent's question goes through the approval layer: the user in ask and safe mode, memory in autonomous."""

from __future__ import annotations

import asyncio

import pytest

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from coding_agents import GateSession
from coding_agents.ask import Question
from config.approval_mode import YES_ANSWER, ApprovalMode
from orchestrator import coding_ask
from orchestrator.coding_ask import ApprovalsAsker
from tests.conftest import StubDecider, approval_policy, deciding

SESSION = GateSession("tok", "run-1", "t1", "/wt")


class Log:
    """Where the run is shown waiting and where `ask` is written, remembered in order."""

    def __init__(self) -> None:
        self.waiting_changes: list[bool] = []
        self.events: list[tuple[str, dict]] = []

    async def waiting(self, run_id: str, waiting: bool) -> None:
        self.waiting_changes.append(waiting)

    async def record(self, run_id: str, task_id: str, event: str, data) -> None:
        self.events.append((event, dict(data)))


@pytest.fixture(autouse=True)
def _quick_waiting_marker(monkeypatch) -> None:
    monkeypatch.setattr(coding_ask, "_WAITING_AFTER_SECONDS", 0.01)


def _asker(mode: ApprovalMode, decider=None, *, store=None, log=None, **kw):
    store = store or ApprovalStore()
    log = log or Log()
    policy = approval_policy(mode, decider=decider)
    return ApprovalsAsker(UserInteraction(store, policy=policy), log, **kw), store, log


async def _user_answers(store: ApprovalStore, text: str, *, by: str = "you") -> None:
    """The person at the dashboard: wait for the card, then answer it."""
    for _ in range(500):
        for card in store.pending():
            store.resolve(card.id, ApprovalDecision.ANSWERED, chosen_option=text, decided_by=by)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no card was raised")


@pytest.mark.parametrize("mode", [ApprovalMode.ASK, ApprovalMode.SAFE])
async def test_in_ask_and_safe_mode_the_user_is_asked_and_their_answer_goes_back(mode) -> None:
    asker, store, log = _asker(mode)

    reply, _ = await asyncio.gather(
        asker.ask(SESSION, Question("Tabs or spaces?", ("tabs", "spaces"))), _user_answers(store, "spaces")
    )

    assert (reply.text, reply.by, reply.answered) == ("spaces", "The user", True)
    [card] = store.all()
    assert card.type.value == "question" and card.task_id == "t1" and card.agent == "coding_agent"
    assert card.message == "Tabs or spaces?" and card.options == ["tabs", "spaces"] and card.blocking


async def test_the_card_carries_the_owning_task_so_the_run_frees_its_slot_while_it_waits() -> None:
    asker, store, _ = _asker(ApprovalMode.ASK)

    async def peek() -> str:
        for _ in range(500):
            if store.pending():
                task = store.pending()[0].task_id
                store.resolve(store.pending()[0].id, ApprovalDecision.ANSWERED, chosen_option="x", decided_by="you")
                return task
            await asyncio.sleep(0.01)
        return ""

    _, task_id = await asyncio.gather(asker.ask(SESSION, Question("Which?")), peek())

    assert task_id == "t1"


async def test_the_run_shows_as_waiting_only_while_nobody_has_answered() -> None:
    asker, store, log = _asker(ApprovalMode.ASK)

    async def slow_answer() -> None:
        await asyncio.sleep(0.1)
        await _user_answers(store, "spaces")

    await asyncio.gather(asker.ask(SESSION, Question("Which?")), slow_answer())

    assert log.waiting_changes == [True, False]


async def test_in_autonomous_mode_north_answers_from_memory_and_says_why_without_asking_anyone() -> None:
    decider = deciding(ApprovalDecision.ANSWERED, "spaces", "you use spaces everywhere", ("uses spaces",))
    asker, store, log = _asker(ApprovalMode.AUTONOMOUS, decider)

    reply = await asker.ask(SESSION, Question("Tabs or spaces?", ("tabs", "spaces")))

    assert reply.text == "spaces" and reply.reason == "you use spaces everywhere"
    assert reply.by.startswith("North, answering for the user from what it knows")
    assert store.pending() == [], "nobody was asked"
    assert log.waiting_changes == [], "an answer that comes at once is not a wait"
    [(event, data)] = log.events
    assert event == "ask" and data["by"] == "memory_decider" and data["answer"] == "spaces"


async def test_in_autonomous_mode_a_decider_that_cannot_answer_leaves_the_card_for_the_user() -> None:
    asker, store, _ = _asker(ApprovalMode.AUTONOMOUS, StubDecider(None))

    reply, _ = await asyncio.gather(asker.ask(SESSION, Question("Which db?")), _user_answers(store, "postgres"))

    assert reply.text == "postgres" and reply.by == "The user"


async def test_yolo_answers_yes() -> None:
    asker, store, _ = _asker(ApprovalMode.YOLO)

    reply = await asker.ask(SESSION, Question("Proceed?"))

    assert reply.text == YES_ANSWER and "yolo" in reply.by and store.pending() == []


async def test_an_answer_that_comes_back_empty_tells_the_agent_to_state_its_assumption() -> None:
    asker, store, log = _asker(ApprovalMode.ASK)

    reply, _ = await asyncio.gather(asker.ask(SESSION, Question("Which?")), _user_answers(store, "  "))

    assert reply.answered is False and "say in your final report exactly what you assumed" in reply.text
    assert log.events[0][1]["answer"] == ""


async def test_every_question_and_its_answer_is_written_on_the_run() -> None:
    asker, store, log = _asker(ApprovalMode.ASK)

    await asyncio.gather(asker.ask(SESSION, Question("Tabs or spaces?")), _user_answers(store, "spaces"))

    [(event, data)] = log.events
    assert event == "ask"
    assert data["question"] == "Tabs or spaces?" and data["answer"] == "spaces" and data["by"] == "you"
    assert data["card"] == store.all()[0].id


async def test_a_long_question_and_answer_are_cut_in_the_record_not_in_the_reply() -> None:
    asker, store, log = _asker(ApprovalMode.ASK)
    long = "x" * 1_000

    reply, _ = await asyncio.gather(asker.ask(SESSION, Question("q" * 1_000)), _user_answers(store, long))

    assert reply.text == long
    assert len(log.events[0][1]["question"]) == 300 and len(log.events[0][1]["answer"]) == 300


async def test_a_run_has_a_limit_of_questions_and_the_agent_is_told_to_use_its_own_judgement() -> None:
    asker, store, log = _asker(ApprovalMode.AUTONOMOUS, deciding(ApprovalDecision.ANSWERED, "ok"), max_asks=2)

    replies = [await asker.ask(SESSION, Question(f"q{n}")) for n in range(3)]

    assert [r.answered for r in replies] == [True, True, False]
    assert "limit" in replies[2].text and "own judgement" in replies[2].text
    assert [e for e, _ in log.events] == ["ask", "ask", "ask_refused"]
    assert len(store.all()) == 2, "the refused question never became a card"


async def test_the_limit_is_per_run() -> None:
    asker, _, _ = _asker(ApprovalMode.AUTONOMOUS, deciding(ApprovalDecision.ANSWERED, "ok"), max_asks=1)
    other = GateSession("tok2", "run-2", "t1", "/wt")

    first = await asker.ask(SESSION, Question("a"))
    second_run = await asker.ask(other, Question("b"))

    assert first.answered and second_run.answered


class Stream:
    """The channel the dashboard and TUI listen on; remembers what was emitted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, task_id: str, event: str, data: dict) -> None:
        self.events.append((task_id, event, data))


async def test_the_user_sees_a_question_not_an_approval_with_the_agents_words_and_options() -> None:
    store, stream = ApprovalStore(), Stream()
    interaction = UserInteraction(store, stream_manager=stream, policy=approval_policy(ApprovalMode.ASK))
    asker = ApprovalsAsker(interaction, Log())

    await asyncio.gather(
        asker.ask(SESSION, Question("Tabs or spaces?", ("tabs", "spaces"))), _user_answers(store, "spaces")
    )

    [(task_id, event, data)] = stream.events
    assert (task_id, event) == ("t1", "question_required")
    assert data["question"] == "Tabs or spaces?" and data["options"] == ["tabs", "spaces"]
