"""The Memory page reads four kinds of memory. Two of them had no endpoint at all.

north was consolidating episodes and learning approval decisions that nobody could
look at - and, in the approval case, nobody could withdraw.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import web.api as web_api
from approval.approval_memory import ApprovalMemory
from orchestrator.api_context import ApiServices, bind_services


@pytest.fixture
def memory(tmp_path) -> ApprovalMemory:
    return ApprovalMemory(tmp_path / "approval_memory.db")


@pytest.mark.asyncio
async def test_learned_decisions_are_readable(memory: ApprovalMemory) -> None:
    memory.record("coder", "git push origin main", "rejected")
    memory.record("home", "turn off the kitchen light", "approved")

    with bind_services(ApiServices(approval_memory=memory)):
        rows = await web_api.memory_approvals()

    by_agent = {row["agent"]: row for row in rows}
    assert by_agent["coder"]["decision"] == "rejected"
    assert by_agent["home"]["decision"] == "approved"
    # The signature is what makes a row mean something to a person.
    assert "push" in by_agent["coder"]["signature"]


@pytest.mark.asyncio
async def test_a_decision_can_be_withdrawn(memory: ApprovalMemory) -> None:
    """The counterpart to recording. Consent that cannot be taken back is not consent."""
    memory.record("home", "turn off the kitchen light", "approved")
    assert memory.recall("home", "turn off the kitchen light") == "approved"

    with bind_services(ApiServices(approval_memory=memory)):
        rows = await web_api.memory_approvals()
        await web_api.forget_memory_approval(rows[0]["fingerprint"])

    # Gone from the cache too, so the very next action asks again.
    assert memory.recall("home", "turn off the kitchen light") is None
    assert memory.all_decisions() == []


@pytest.mark.asyncio
async def test_forgetting_something_unknown_is_a_404(memory: ApprovalMemory) -> None:
    with bind_services(ApiServices(approval_memory=memory)), pytest.raises(HTTPException) as exc:
        await web_api.forget_memory_approval("nothing-like-this")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_views_are_empty_rather_than_broken_when_unwired() -> None:
    """A page must render on an install where these stores were never built."""
    with bind_services(ApiServices()):
        assert await web_api.memory_approvals() == []
        assert await web_api.memory_episodes() == []


@pytest.mark.asyncio
async def test_episodes_are_listed_newest_first_without_their_vectors(tmp_path) -> None:
    from memory.episodic import EpisodicStore

    store = EpisodicStore(db_path=tmp_path / "episodic.db")
    await store.record("task_a", "home", "Dimmed the lights", outcome="success")
    await store.record("task_b", "job", "Asked about applying for jobs", outcome="cancelled")

    with bind_services(ApiServices(episodic_store=store)):
        rows = await web_api.memory_episodes(limit=10)

    assert [row["task_id"] for row in rows] == ["task_b", "task_a"]
    # Several kilobytes per row that mean nothing to a reader.
    assert all("embedding" not in row for row in rows)
