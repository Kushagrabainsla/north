"""Judgement rules and north stars live in their documents only; the profile shows them from there.

They used to be written twice, into the document (read by the approval decider,
the goal check and the Documents tab) and into the fact store (read by the
profile and recall). Editing the document changed one copy and left the other.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import orchestrator.api.context as context_api
from memory import FileContextStore
from memory.facts import FactStore
from memory.injection import ContextInjector
from memory.models import ContextDocument
from memory.profile import move_rule_and_goal_facts, rebuild_profile
from orchestrator.api_context import ApiServices, bind_services


async def _no_vectors(texts: list[str]) -> list[list[float]]:
    return [[] for _ in texts]


@pytest.fixture
def facts(tmp_path: Path) -> FactStore:
    return FactStore(db_path=tmp_path / "facts.db", embed_fn=_no_vectors)


@pytest.fixture
def documents(tmp_path: Path) -> FileContextStore:
    return FileContextStore(tmp_path / "context")


@pytest.mark.asyncio
async def test_the_profile_shows_facts_and_the_goal_and_rule_documents(facts, documents) -> None:
    await facts.add_fact("User prefers tea", category="preferences")
    await documents.write(ContextDocument.NORTH_STARS, "User wants to lead a team by 2028.")
    await documents.write(ContextDocument.JUDGEMENT_RULES, "User rejects roles that need relocation to New York.")

    await rebuild_profile(facts, documents)

    profile = await documents.read(ContextDocument.USER)
    assert "User prefers tea" in profile
    assert "## North stars (your goals)\nUser wants to lead a team by 2028." in profile
    assert "## Judgement rules\nUser rejects roles that need relocation to New York." in profile


@pytest.mark.asyncio
async def test_without_a_fact_store_the_profile_is_left_alone(documents) -> None:
    await documents.write(ContextDocument.USER, "# User Profile\n- built earlier")

    await rebuild_profile(None, documents)

    assert await documents.read(ContextDocument.USER) == "# User Profile\n- built earlier"


@pytest.mark.asyncio
async def test_old_copies_move_into_their_documents_once_and_nothing_is_lost(facts, documents) -> None:
    await documents.write(ContextDocument.JUDGEMENT_RULES, "User prefers US-wide full-time roles.\n")
    await facts.add_fact("User prefers US-wide full-time roles.", category="judgement_rules")  # already there
    await facts.add_fact("User only uses notifications with a view jobs button.", category="judgement_rules")
    await facts.add_fact("User wants a frontier-lab role.", category="north_stars")
    await facts.add_fact("User wants to finish the summer plan.", category="north_stars", valid_until="2020-01-01")
    await facts.add_fact("User prefers tea", category="preferences")

    assert await move_rule_and_goal_facts(facts, documents) == 4
    assert await move_rule_and_goal_facts(facts, documents) == 0

    rules = await documents.read(ContextDocument.JUDGEMENT_RULES)
    goals = await documents.read(ContextDocument.NORTH_STARS)
    assert rules.count("US-wide full-time roles") == 1
    assert "view jobs button" in rules
    assert "frontier-lab role" in goals and "summer plan" not in goals  # an ended goal is not revived
    assert [fact["category"] for fact in await facts.all_facts()] == ["preferences"]


@pytest.mark.asyncio
async def test_editing_a_rule_on_the_documents_tab_updates_the_profile(facts, documents) -> None:
    with bind_services(ApiServices(context_store=documents, fact_store=facts)):
        await context_api.write_context(
            "judgement_rules", context_api.ContextWriteRequest(content="User avoids Friday deploys.")
        )

    assert "User avoids Friday deploys." in await documents.read(ContextDocument.USER)


@pytest.mark.asyncio
async def test_the_profile_cannot_be_edited_directly(facts, documents) -> None:
    """It is rebuilt from facts and documents, so an edit was silently erased at the next rebuild."""
    with bind_services(ApiServices(context_store=documents, fact_store=facts)), pytest.raises(HTTPException) as error:
        await context_api.write_context("user", context_api.ContextWriteRequest(content="hand edit"))

    assert error.value.status_code == 409 and "Facts tab" in error.value.detail


@pytest.mark.asyncio
async def test_something_about_you_added_by_hand_becomes_a_fact(facts, documents) -> None:
    router = MagicMock()
    router.complete = AsyncMock(
        return_value=SimpleNamespace(text='{"document": "user", "delta": "User lives in San Jose."}')
    )
    injector = ContextInjector(documents, router, MagicMock(write=AsyncMock()), fact_store=facts)

    await injector.inject_text("I live in San Jose")

    assert [fact["content"] for fact in await facts.all_facts()] == ["User lives in San Jose."]
    assert "User lives in San Jose." in await documents.read(ContextDocument.USER)  # via the rebuilt profile
