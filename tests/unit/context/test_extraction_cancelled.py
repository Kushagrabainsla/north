"""North learns from your messages once their exchange is over, and never from a cancelled one.

Cancelling is the user saying the exchange does not count. Everything said inside
it goes with it - including, especially, answers to north's own clarifying
questions, which are user-authored and individually marked completed, so only the
task's ending records that they were withdrawn.

Extraction also reads the whole backlog in one run: it once read 50 ledger rows
per finished task while tasks wrote more than that, and fell two weeks behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import memory.extraction as extraction
from ledger import SQLiteLedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import FileContextStore
from memory.extraction import ExtractionPipeline, learning_status
from utils.ids import generate_id
from utils.time import utcnow

_ANSWER = (
    "north asked: Do you want me to submit only after you approve each application, or give "
    "standing approval for roles that match? The user answered: Standing approval for matching roles"
)


class _Model:
    """Reads what extraction asks about; finds nothing to store, so no store is touched."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def complete(self, request) -> SimpleNamespace:
        self.asked.append(request.prompt)
        return SimpleNamespace(text='{"extract": false}')

    def read(self, text: str) -> bool:
        return any(text in prompt for prompt in self.asked)


class _Log:
    """A real ledger, written in order one microsecond apart, as a task would write it."""

    def __init__(self, path: Path) -> None:
        self.ledger = SQLiteLedgerWriter(path)
        self._clock = utcnow() - timedelta(minutes=5)

    async def say(self, task: str | None, text: str, *, source=LedgerSource.PROMPT, at: datetime | None = None):
        await self.write(source, task, input=text, action="task_received", status=LedgerStatus.COMPLETED, at=at)

    async def end(self, task: str, action: str = "task_completed", status=LedgerStatus.COMPLETED):
        await self.write(LedgerSource.SYSTEM, task, action=action, status=status)

    async def noise(self, task: str, rows: int) -> None:
        for _ in range(rows):
            await self.write(
                LedgerSource.INFERENCE_ROUTER, task, action="inference_call", status=LedgerStatus.COMPLETED
            )

    async def write(self, source, task, *, at: datetime | None = None, **fields) -> None:
        self._clock += timedelta(microseconds=1)
        await self.ledger.write(
            LedgerEntry(id=generate_id(), timestamp=at or self._clock, source=source, task_id=task, **fields)
        )


@pytest.fixture
def log(tmp_path: Path) -> _Log:
    return _Log(tmp_path / "ledger.db")


@pytest.fixture
def model() -> _Model:
    return _Model()


@pytest.fixture
def pipeline(tmp_path: Path, log: _Log, model: _Model) -> ExtractionPipeline:
    return ExtractionPipeline(log.ledger, FileContextStore(tmp_path / "context"), model, tmp_path)


@pytest.mark.asyncio
async def test_answers_from_a_cancelled_task_are_dropped(log, model, pipeline) -> None:
    """The real case: a job-application run answered four questions, then was cancelled.

    Extraction wrote "User gives standing approval for matching roles" into
    judgement_rules - a consent taken from a conversation the user walked out of.
    """
    await log.say("task_jobs", _ANSWER, source=LedgerSource.CLARIFICATION)
    await log.end("task_jobs", "task_cancelled", LedgerStatus.CANCELLED)

    await pipeline.run_once()

    assert model.asked == []


@pytest.mark.asyncio
async def test_a_cancellation_in_a_later_batch_still_counts(log, model, pipeline, monkeypatch) -> None:
    """Where the batch boundary falls must not decide it: the ending is looked up per task."""
    monkeypatch.setattr(extraction, "_BATCH_SIZE", 3)
    await log.say("task_jobs", _ANSWER, source=LedgerSource.CLARIFICATION)
    await log.noise("task_other", 7)
    await log.end("task_jobs", "task_cancelled", LedgerStatus.CANCELLED)

    await pipeline.run_once()

    assert model.asked == []


@pytest.mark.asyncio
async def test_another_task_is_untouched(log, model, pipeline) -> None:
    """One abandoned conversation must not erase what was said in a different one."""
    await log.say("task_jobs", _ANSWER, source=LedgerSource.CLARIFICATION)
    await log.end("task_jobs", "task_cancelled", LedgerStatus.CANCELLED)
    await log.say("task_other", "I prefer window seats on long flights")
    await log.end("task_other")

    await pipeline.run_once()

    assert len(model.asked) == 1 and model.read("window seats")


@pytest.mark.asyncio
async def test_a_message_with_no_task_is_read(log, model, pipeline) -> None:
    """`task_id` is None for plenty of entries; there is no ending to wait for."""
    await log.say(None, "I work at an infrastructure company")

    await pipeline.run_once()

    assert model.read("infrastructure company")


@pytest.mark.asyncio
async def test_a_message_waits_until_its_task_ends(log, model, pipeline) -> None:
    """A task still running may yet be cancelled, so its messages wait, and so does what follows."""
    await log.say("task_open", "I always book aisle seats")
    await log.say("task_done", "I prefer morning meetings")
    await log.end("task_done")

    await pipeline.run_once()
    assert model.asked == []

    await log.end("task_open")
    await pipeline.run_once()

    assert model.read("aisle seats") and model.read("morning meetings")


@pytest.mark.asyncio
async def test_a_task_with_no_ending_after_a_day_counts(log, model, pipeline) -> None:
    """North stopped mid-task: nothing will ever end it, and learning must not wait forever."""
    await log.say("task_cut_off", "My manager is Priya", at=utcnow() - timedelta(days=2))

    await pipeline.run_once()

    assert model.read("My manager is Priya")


@pytest.mark.asyncio
async def test_one_run_reads_a_backlog_longer_than_a_batch(log, model, pipeline, monkeypatch) -> None:
    """The bug: 50 rows per finished task, while a task wrote more, so reading never caught up."""
    monkeypatch.setattr(extraction, "_BATCH_SIZE", 3)
    for n in range(10):
        await log.say(f"task_{n}", f"Fact number {n} about my week")
        await log.noise(f"task_{n}", 4)
        await log.end(f"task_{n}")

    await pipeline.run_once()

    assert len(model.asked) == 10
    assert (await learning_status(log.ledger, pipeline._north_home))["waiting"] == 0


@pytest.mark.asyncio
async def test_learning_status_counts_messages_not_read_yet(log, pipeline) -> None:
    await log.say("task_a", "I like trains")
    await log.say("task_b", "I like boats")
    await log.end("task_a")

    before = await learning_status(log.ledger, pipeline._north_home)
    await pipeline.run_once()
    after = await learning_status(log.ledger, pipeline._north_home)

    assert before == {"read_up_to": None, "waiting": 2, "more": False}
    assert after["waiting"] == 1 and after["read_up_to"] is not None  # task_b is still running


@pytest.mark.parametrize("status", [LedgerStatus.FAILED, LedgerStatus.CANCELLED])
@pytest.mark.asyncio
async def test_a_message_that_did_not_go_through_teaches_nothing(log, model, pipeline, status) -> None:
    await log.write(LedgerSource.PROMPT, "t", input="I always deploy on Fridays", status=status)
    await log.end("t", "task_failed", LedgerStatus.FAILED)

    await pipeline.run_once()

    assert model.asked == []
