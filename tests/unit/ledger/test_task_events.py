"""Whole tasks from the ledger: the newest N tasks, every entry of each, searched in full (#26).

The Tasks page asked for the latest 500 entries, and those were 16 of 421 tasks: about a day and a
half. Older tasks could not be opened from the list or found by its search.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource
from ledger.sqlite_writer import SQLiteLedgerWriter

START = datetime(2026, 11, 1, tzinfo=UTC)


def _entries() -> list[LedgerEntry]:
    """Three tasks of three entries each, oldest first; task 0 is the oldest."""
    prompts = ["Remind me about the paneer tikka", "Summarise the nightly check", "Book a flight to Delhi"]
    rows = []
    for number, prompt in enumerate(prompts):
        at = START + timedelta(days=number)
        task = f"t{number}"
        rows += [
            LedgerEntry(
                id=f"{task}a",
                timestamp=at,
                source=LedgerSource.PROMPT,
                task_id=task,
                action="task_received",
                input=prompt,
            ),
            LedgerEntry(
                id=f"{task}b",
                timestamp=at + timedelta(minutes=1),
                source=LedgerSource.AGENT,
                task_id=task,
                agent="north",
                action="agent_completed",
            ),
            LedgerEntry(
                id=f"{task}c",
                timestamp=at + timedelta(minutes=2),
                source=LedgerSource.AGENT,
                task_id=task,
                action="task_completed",
            ),
        ]
    rows.append(
        LedgerEntry(id="sys", timestamp=START + timedelta(days=9), source=LedgerSource.SYSTEM, action="startup")
    )
    return rows


class _ListLedger(LedgerWriter):
    """The interface's own default, over a list, as a store without SQL has it."""

    def __init__(self, entries: list[LedgerEntry]) -> None:
        self._entries = entries

    async def write(self, entry: LedgerEntry) -> str:
        self._entries.append(entry)
        return entry.id

    async def get(self, entry_id: str) -> LedgerEntry | None:
        return next((e for e in self._entries if e.id == entry_id), None)

    async def query(self, filters: LedgerFilters) -> list[LedgerEntry]:
        return sorted(self._entries, key=lambda e: e.timestamp, reverse=True)[: filters.limit]


@pytest.fixture(params=["sqlite", "default"])
async def ledger(request, tmp_path) -> LedgerWriter:
    if request.param == "default":
        return _ListLedger(_entries())
    store = SQLiteLedgerWriter(tmp_path / "ledger.db")
    for entry in _entries():
        await store.write(entry)
    return store


def _tasks(entries: list[LedgerEntry]) -> list[str]:
    return list(dict.fromkeys(entry.task_id for entry in entries))


async def test_the_newest_tasks_come_whole_and_newest_first(ledger) -> None:
    entries = await ledger.task_events(2)

    assert _tasks(entries) == ["t2", "t1"]
    assert len(entries) == 6, "every entry of each task, its prompt included"
    assert [e.timestamp for e in entries] == sorted((e.timestamp for e in entries), reverse=True)


async def test_an_entry_with_no_task_is_not_a_task(ledger) -> None:
    assert "sys" not in {entry.id for entry in await ledger.task_events(10)}


async def test_a_search_finds_an_old_task_by_a_word_it_starts(ledger) -> None:
    """A search box sends what you typed so far: "panee" finds "paneer"."""
    entries = await ledger.task_events(10, "panee tikka")

    assert _tasks(entries) == ["t0"] and len(entries) == 3


async def test_a_search_that_matches_nothing_finds_no_task(ledger) -> None:
    assert await ledger.task_events(10, "zebra") == []


async def test_punctuation_in_a_search_splits_words_as_the_index_does(tmp_path) -> None:
    """Deleting it made "Don't" into "Dont" and "fizzbuzz(n)" into "fizzbuzzn", which no entry holds."""
    store = SQLiteLedgerWriter(tmp_path / "ledger.db")
    prompt = "Don't change fizzbuzz(n) or count_occurrences(text, word)"
    await store.write(
        LedgerEntry(
            id="p", timestamp=START, source=LedgerSource.PROMPT, task_id="t9", action="task_received", input=prompt
        )
    )

    for typed in ("Don't change", "fizzbuzz(n)", "count_occurrences(text, word)"):
        assert _tasks(await store.task_events(10, typed)) == ["t9"], typed
        assert [hit.entry.id for hit in await store.search(typed)] == ["p"], typed
