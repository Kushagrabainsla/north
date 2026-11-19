"""A task's note, and skills from the notes, are learned when the task ends, not at the next hourly run."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import memory.consolidator as consolidator_module
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory.consolidator import EpisodeConsolidator
from memory.episodic import EpisodicStore
from memory.task_end_learning import TaskEndLearning
from orchestrator.journal import TaskJournal
from tests.unit.memory.test_consolidator import _Answering, _FakeLedger, _task


class _Stored:
    def __init__(self, fails: bool = False) -> None:
        self.rows: list[LedgerEntry] = []
        self._fails = fails

    async def write(self, entry: LedgerEntry) -> None:
        if self._fails:
            raise OSError("disk full")
        self.rows.append(entry)


def _row(action: str, source: LedgerSource = LedgerSource.SYSTEM) -> LedgerEntry:
    return LedgerEntry.new(source=source, task_id="t1", action=action, status=LedgerStatus.COMPLETED)


async def test_the_journal_says_a_task_ended_only_for_its_ending_row_once_stored() -> None:
    ledger = _Stored()
    journal = TaskJournal(ledger=ledger, stream_manager=None)  # type: ignore[arg-type]
    ended: list[tuple[str, int]] = []
    journal.when_ended(lambda task_id: ended.append((task_id, len(ledger.rows))))

    await journal.write(_row("task_received", LedgerSource.PROMPT))
    await journal.write(_row("agent_completed", LedgerSource.AGENT))
    await journal.write(_row("task_completed"))

    assert ended == [("t1", 3)]


@pytest.mark.parametrize("action", ["task_failed", "task_cancelled", "task_stuck", "task_needs_attention"])
async def test_every_kind_of_ending_says_so(action: str) -> None:
    journal = TaskJournal(ledger=_Stored(), stream_manager=None)  # type: ignore[arg-type]
    ended: list[str] = []
    journal.when_ended(ended.append)

    await journal.write(_row(action))

    assert ended == ["t1"]


async def test_an_ending_that_was_not_stored_and_a_broken_listener_change_nothing() -> None:
    journal = TaskJournal(ledger=_Stored(fails=True), stream_manager=None)  # type: ignore[arg-type]
    ended: list[str] = []
    journal.when_ended(ended.append)
    await journal.write(_row("task_completed"))
    assert ended == []  # nothing to learn from: the row is not there

    working = TaskJournal(ledger=_Stored(), stream_manager=None)  # type: ignore[arg-type]
    working.when_ended(lambda _task_id: 1 / 0)
    working.when_ended(ended.append)
    await working.write(_row("task_completed"))
    assert ended == ["t1"]


class _Notes:
    def __init__(self, recorded: int = 1) -> None:
        self.runs = 0
        self.recorded = recorded
        self.release = asyncio.Event()

    async def catch_up(self) -> int:
        self.runs += 1
        await self.release.wait()
        return self.recorded


class _Skills:
    def __init__(self) -> None:
        self.runs = 0

    async def run_once(self) -> int:
        self.runs += 1
        return 0


async def _settle(learning: TaskEndLearning) -> None:
    assert learning._running is not None
    await asyncio.wait_for(learning._running, timeout=2)


async def test_ends_during_a_run_ask_for_one_more_run_not_one_each() -> None:
    notes, skills = _Notes(), _Skills()
    learning = TaskEndLearning(notes, skills)

    learning.task_ended("a")
    await asyncio.sleep(0)
    learning.task_ended("b")
    learning.task_ended("c")
    notes.release.set()
    await _settle(learning)

    assert notes.runs == 2
    assert skills.runs == 2


async def test_skills_are_learned_only_when_a_new_note_was_written() -> None:
    notes, skills = _Notes(recorded=0), _Skills()
    notes.release.set()
    learning = TaskEndLearning(notes, skills)

    learning.task_ended("a")
    await _settle(learning)

    assert (notes.runs, skills.runs) == (1, 0)


async def test_a_failed_run_does_not_stop_the_next_end_from_learning() -> None:
    class _Broken(_Notes):
        async def catch_up(self) -> int:
            self.runs += 1
            raise RuntimeError("model away")

    notes, skills = _Broken(), _Skills()
    learning = TaskEndLearning(notes, skills)
    learning.task_ended("a")
    await _settle(learning)
    learning.task_ended("b")
    await _settle(learning)

    assert notes.runs == 2


async def test_catching_up_reads_past_one_batch(tmp_path: Path, monkeypatch) -> None:
    """A long task writes more rows than one batch holds; its ending row must still be read."""
    monkeypatch.setattr(consolidator_module, "_BATCH_SIZE", 2)
    rows = [row for index in range(3) for row in _task(f"task-{index}", f"Write report {index}.")]
    store = EpisodicStore(tmp_path / "episodic.db")
    consolidator = EpisodeConsolidator(_FakeLedger(rows), store, _Answering(), tmp_path)

    assert await consolidator.catch_up() == 3
    assert len(await store.recent()) == 3
    assert await consolidator.catch_up() == 0
