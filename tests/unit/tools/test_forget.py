"""Asked to forget something, north can: it asks first, then removes exactly what it listed.

In the issue #76 test "pls forget my favourite food, i dont want you keeping that"
got "I can't delete that from my memory. I have no tool that edits or removes
stored personal facts", and the fact stayed. Only the Memory page could remove
one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.approval_mode import ApprovalMode
from memory import ContextDocument, FileContextStore
from memory.facts import FactStore
from memory.profile import rebuild_profile
from tests.conftest import approving_store, bind_approvals, rejecting_store
from tools.models import ToolInput
from tools.universal.forget import ForgetTool

_T0 = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)


async def _no_embeddings(texts: list[str]) -> list[list[float]]:
    raise RuntimeError("embedding model unavailable")


@pytest.fixture
async def memory(tmp_path: Path) -> tuple[FactStore, FileContextStore]:
    facts = FactStore(tmp_path / "facts.db", _no_embeddings)
    documents = FileContextStore(tmp_path / "context")
    await facts.add_fact("User's name is Sam", about="name", said_at=_T0)
    await facts.add_fact("User's favourite food is ramen", about="favourite food", said_at=_T0)
    await facts.add_fact("User's favourite food is pho", about="favourite food", said_at=_T0 + timedelta(1))
    await facts.add_fact("User is not allergic to penicillin.", about="penicillin allergy", said_at=_T0)
    await rebuild_profile(facts, documents)
    return facts, documents


def _card(store) -> str:
    return store.add.call_args.args[0].message


async def _contents(facts: FactStore) -> set[str]:
    return {fact["content"] for fact in await facts.all_facts()}


async def test_forgetting_asks_with_the_exact_list_then_removes_it(memory) -> None:
    facts, documents = memory
    store = approving_store()
    tool = bind_approvals(ForgetTool(facts, documents), store=store)

    out = await tool.execute(ToolInput(params={"what": "favourite food", "task_id": "t1"}))

    assert out.success, out.error
    card = _card(store)
    assert "User's favourite food is pho" in card
    assert "User's favourite food is ramen (an earlier value)" in card
    assert "Sam" not in card and "penicillin" not in card
    # The current value and the earlier one are both gone, and nothing else.
    assert await _contents(facts) == {"User's name is Sam", "User is not allergic to penicillin."}
    assert "pho" not in await documents.read(ContextDocument.USER)


async def test_a_no_forgets_nothing(memory) -> None:
    facts, documents = memory
    tool = bind_approvals(ForgetTool(facts, documents), store=rejecting_store())

    out = await tool.execute(ToolInput(params={"what": "favourite food", "task_id": "t1"}))

    assert out.failure_kind == "refused"
    assert "User's favourite food is pho" in await _contents(facts)


async def test_it_asks_even_when_nothing_else_would(memory) -> None:
    """Forgetting cannot be undone: no mode, rule or earlier yes decides it but a person."""
    facts, documents = memory
    store = rejecting_store()
    tool = bind_approvals(ForgetTool(facts, documents), mode=ApprovalMode.YOLO, store=store)

    await tool.execute(ToolInput(params={"what": "penicillin allergy", "task_id": "t1"}))

    store.wait_for_decision.assert_awaited_once()
    assert "User is not allergic to penicillin." in await _contents(facts)


async def test_nothing_matching_says_so_and_asks_nothing(memory) -> None:
    facts, documents = memory
    store = approving_store()
    tool = bind_approvals(ForgetTool(facts, documents), store=store)

    out = await tool.execute(ToolInput(params={"what": "dentist", "task_id": "t1"}))

    assert out.failure_kind == "not_found"
    assert "nothing was forgotten" in out.error
    store.add.assert_not_called()
    assert len(await _contents(facts)) == 4


def test_a_correction_is_not_a_reason_to_forget() -> None:
    """Told "wait im not allergic to penicillin", the agent called forget and raised a card.

    north replaces a corrected fact by itself once the task ends, so the agent is
    told to leave corrections alone and to forget only when asked to.
    """
    prompt = (Path(__file__).parents[3] / "agents" / "general" / "prompts" / "system.md").read_text(encoding="utf-8")

    assert "replaces the old fact by itself, so acknowledge it and do nothing more" in prompt
    assert "Call `forget` only when the user asks you to forget something" in prompt
    assert "Not for a correction or an update" in ForgetTool.description
