"""Several questions on one card, answered together.

Setting up a flow asked its open choices one `ask_user` call at a time, or as a numbered
list in one question's text that the user answered in one box. Now each question is a
field of one card: the dashboard shows them with their options as buttons and one Send
answers button, Telegram and the terminal take one line per question, and the agent gets
every answer back at once. The answers are the card's `chosen_option` too, so the ledger,
past decisions and a task that starts over read them all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.models import AgentPayload
from agents.schemas import ASK_USER_SCHEMA
from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.store import ApprovalStore
from approval.telegram import TelegramNotifier
from cli.tui import _question_text
from config.approval_mode import YES_ANSWER, ApprovalMode
from ledger import LedgerSource
from tests.conftest import StubDecider, deciding
from tests.unit.agents.test_autonomous_ask_user import _agent
from tests.unit.approval.test_overrule import _orchestrator
from utils.runtime_resources import builtin_skills_dir

_QUESTIONS = [
    {"question": "When should this flow run?", "options": ["Manual only", "Once at a specific time", "Recurring"]},
    {"question": "Which folder holds the articles?"},
]


def _ask(agent, questions=_QUESTIONS, **extra):
    params = {"question": "Before I set up the digest:", "questions": questions, **extra}
    return agent._ask_user(AgentPayload(task_id="t1", prompt="p"), params)


async def _asked(tmp_path: Path, store: ApprovalStore) -> tuple[asyncio.Task, Card]:
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)
    asking = asyncio.create_task(_ask(agent))
    await asyncio.sleep(0)
    [card] = store.pending()
    return asking, card


def _card() -> Card:
    fields = [
        CardField(name="q1", label="When should this flow run?", editable=True, options=["Manual only", "Recurring"]),
        CardField(name="q2", label="Which folder?", editable=True),
    ]
    return Card.new(type=CardType.QUESTION, agent="general", title="Q", message="Setup", fields=fields)


async def test_the_questions_are_one_card_and_every_answer_comes_back(tmp_path: Path) -> None:
    store = ApprovalStore()
    asking, card = await _asked(tmp_path, store)

    assert [field.label for field in card.fields] == ["When should this flow run?", "Which folder holds the articles?"]
    assert card.fields[0].options == ["Manual only", "Once at a specific time", "Recurring"]
    assert card.message == "Before I set up the digest:"
    store.resolve(card.id, "answered", values={"q1": "Manual only", "q2": "~/Reading"}, decided_by="you")

    out = json.loads(await asking)
    assert out["answers"] == [
        {"question": "When should this flow run?", "answer": "Manual only"},
        {"question": "Which folder holds the articles?", "answer": "~/Reading"},
    ]
    assert out["answered_by"] == "the user"
    assert store.get(card.id).chosen_option == (
        "When should this flow run? → Manual only\nWhich folder holds the articles? → ~/Reading"
    )


async def test_one_message_with_a_line_per_question_answers_each(tmp_path: Path) -> None:
    store = ApprovalStore()
    asking, card = await _asked(tmp_path, store)

    store.resolve(card.id, "answered", chosen_option="1. 3\n2. ~/Reading", decided_by="you")  # Telegram

    answers = [item["answer"] for item in json.loads(await asking)["answers"]]
    assert answers == ["Recurring", "~/Reading"]


def test_one_line_split_by_semicolons_answers_each_and_dates_keep_their_digits() -> None:
    assert _card().answers_in("recurring; ~/Reading") == {"q1": "Recurring", "q2": "~/Reading"}
    assert _card().answers_in("2026-11-05; 1.5 hours") == {"q1": "2026-11-05", "q2": "1.5 hours"}


async def test_a_reply_north_cannot_split_reaches_the_agent_whole(tmp_path: Path) -> None:
    store = ApprovalStore()
    asking, card = await _asked(tmp_path, store)

    store.resolve(card.id, "answered", chosen_option="run it by hand from my Reading folder", decided_by="you")

    out = json.loads(await asking)
    assert out["reply"] == "run it by hand from my Reading folder"
    assert "read each answer" in out["note"]


async def test_yolo_answers_every_question(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.YOLO, StubDecider(None), store)
    yes = [{"question": "Delete the old logs?", "options": ["No", "Yes, delete them"]}, {"question": "Which folder?"}]

    out = json.loads(await _ask(agent, yes))

    assert [item["answer"] for item in out["answers"]] == ["Yes, delete them", YES_ANSWER]
    assert "yolo" in out["answered_by"]


async def test_autonomous_answers_each_question_from_memory(tmp_path: Path) -> None:
    store = ApprovalStore()
    decider = deciding(ApprovalDecision.ANSWERED, "Manual only", "you run digests by hand")
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS, decider, store)

    out = json.loads(await _ask(agent))

    assert [item["answer"] for item in out["answers"]] == ["Manual only", "Manual only"]
    assert [asked.message for asked in decider.asked] == [q["question"] for q in _QUESTIONS]
    assert store.all()[0].reason == "you run digests by hand; you run digests by hand"


async def test_autonomous_leaves_the_card_to_you_when_it_cannot_answer_one(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS, StubDecider(None), store)

    asking = asyncio.create_task(_ask(agent))
    await asyncio.sleep(0)

    [card] = store.pending()
    assert len(card.fields) == 2
    asking.cancel()


async def test_a_handoff_is_not_one_of_several_questions(tmp_path: Path) -> None:
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), ApprovalStore())

    out = json.loads(await _ask(agent, requires_user_action=True))

    assert out["success"] is False and "on its own" in out["error"]


async def test_your_answers_are_learned_as_one_clarification() -> None:
    store = ApprovalStore()
    card = _card()
    store.add(card)
    orch = _orchestrator(store)

    await orch.respond_approval(card.id, "answered", "", values={"q1": "Recurring", "q2": "~/Reading"})

    entry = orch._ledger.write.call_args[0][0]
    assert entry.source is LedgerSource.CLARIFICATION
    assert "When should this flow run? → Recurring" in entry.input and "Which folder? → ~/Reading" in entry.input


def test_telegram_and_the_tui_number_the_questions_and_say_how_to_answer() -> None:
    card = _card()
    telegram = TelegramNotifier._render(card)
    tui = _question_text({"question": card.message, "fields": [f.model_dump(mode="json") for f in card.fields]})

    assert "1. When should this flow run?\n    1) Manual only\n    2) Recurring\n2. Which folder?" in telegram
    assert "one line per question" in telegram
    assert "1. When should this flow run?   1) Manual only / 2) Recurring" in tui
    assert "separated by ;" in tui


def test_the_model_and_the_guides_are_told_to_ask_together() -> None:
    schema = ASK_USER_SCHEMA["function"]
    flow_guide = (builtin_skills_dir() / "authoring-a-north-flow" / "SKILL.md").read_text(encoding="utf-8")

    assert "questions" in schema["parameters"]["properties"]
    assert "ask them together" in schema["description"]
    assert "each as an item of `questions`" in flow_guide


def test_options_are_complete_answers_and_a_detail_is_its_own_question() -> None:
    """In a real run Haiku offered "A folder of files on this computer (tell me the path)": picked, it gave no path."""
    item = ASK_USER_SCHEMA["function"]["parameters"]["properties"]["questions"]["items"]

    assert "not as an option that says 'tell me'" in item["properties"]["options"]["description"]
