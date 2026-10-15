"""Every fact in a message is kept, each where it belongs; user facts live only in FactStore."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import memory.extraction as extraction
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import ContextDocument, FileContextStore
from memory.extraction import ExtractionPipeline
from utils.time import local_timezone, utcnow


def _setup(tmp_path: Path, reply: str):
    inference = AsyncMock()
    inference.complete.return_value = SimpleNamespace(text=reply)
    facts = AsyncMock()
    facts.add_fact.return_value = True
    documents = FileContextStore(tmp_path / "context")
    ledger = AsyncMock()
    pipeline = ExtractionPipeline(ledger, documents, inference, tmp_path, fact_store=facts)
    entry = LedgerEntry(
        id="entry-1",
        timestamp=utcnow(),
        source=LedgerSource.PROMPT,
        input="I start at Acme on Monday, my manager is Priya, and I want to lead a team by 2028",
        status=LedgerStatus.COMPLETED,
    )
    return pipeline, entry, facts, documents, ledger


def _reply(*facts: tuple[str, str]) -> str:
    return json.dumps({"facts": [{"document": document, "delta": delta} for document, delta in facts]})


@pytest.mark.asyncio
async def test_user_fact_does_not_write_user_document(tmp_path: Path) -> None:
    pipeline, entry, facts, documents, _ = _setup(tmp_path, _reply(("user", "User prefers window seats.")))

    assert await pipeline._process_entry(entry) == 1
    facts.add_fact.assert_awaited_once_with("User prefers window seats.", "user", valid_from=None, valid_until=None)
    assert await documents.read(ContextDocument.USER) == ""


@pytest.mark.asyncio
async def test_every_fact_in_a_message_is_kept(tmp_path: Path) -> None:
    """The bug: one fact per message, so the manager and the goal were lost."""
    reply = _reply(
        ("user", "User starts a job at Acme on Monday."),
        ("user", "User's manager at Acme is Priya."),
        ("north_stars", "User wants to lead a team by 2028."),
    )
    pipeline, entry, facts, documents, ledger = _setup(tmp_path, reply)

    assert await pipeline._process_entry(entry) == 3
    assert [call.args for call in facts.add_fact.await_args_list] == [
        ("User starts a job at Acme on Monday.", "user"),
        ("User's manager at Acme is Priya.", "user"),
        ("User wants to lead a team by 2028.", "north_stars"),
    ]
    assert "lead a team by 2028" in await documents.read(ContextDocument.NORTH_STARS)
    assert ledger.write.await_count == 3  # one "extraction: ... updated" record per fact


@pytest.mark.asyncio
async def test_a_fact_already_known_is_not_counted(tmp_path: Path) -> None:
    pipeline, entry, facts, _, ledger = _setup(
        tmp_path, _reply(("user", "User starts a job at Acme on Monday."), ("user", "User's manager is Priya."))
    )
    facts.add_fact.side_effect = [False, True]  # the store already had the first

    assert await pipeline._process_entry(entry) == 1
    assert ledger.write.await_count == 1


@pytest.mark.parametrize(
    "reply",
    [
        '{"facts": []}',
        "not json",
        '["a list, not an object"]',
        '{"facts": "not a list"}',
        '{"facts": [{"document": "unknown_doc", "delta": "x"}, {"document": "user", "delta": "  "}, "loose"]}',
    ],
)
@pytest.mark.asyncio
async def test_no_usable_fact_stores_nothing(tmp_path: Path, reply: str) -> None:
    pipeline, entry, facts, _, _ = _setup(tmp_path, reply)

    assert await pipeline._process_entry(entry) == 0
    facts.add_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_runaway_reply_is_capped(tmp_path: Path) -> None:
    reply = _reply(*[("user", f"User fact number {n}.") for n in range(25)])
    pipeline, entry, facts, _, _ = _setup(tmp_path, reply)

    assert await pipeline._process_entry(entry) == extraction._MAX_FACTS_PER_MESSAGE


@pytest.mark.asyncio
async def test_the_day_said_and_each_facts_dates_reach_the_store(tmp_path: Path) -> None:
    """The model hears when the message was sent, so "Monday" can become a real day."""
    reply = json.dumps(
        {
            "facts": [
                {"document": "user", "delta": "User's internship runs until August 14, 2026.", "until": "2026-08-14"},
                {"document": "user", "delta": "User likes tea.", "from": None, "until": "null"},
            ]
        }
    )
    pipeline, entry, facts, _, _ = _setup(tmp_path, reply)

    assert await pipeline._process_entry(entry) == 2

    prompt = pipeline._inference_router.complete.await_args.args[0].prompt
    assert f"The user said this on {entry.timestamp.astimezone(local_timezone()).strftime('%A')}" in prompt
    first, second = facts.add_fact.await_args_list
    assert first.kwargs == {"valid_from": None, "valid_until": "2026-08-14"}
    assert second.kwargs == {"valid_from": None, "valid_until": None}
