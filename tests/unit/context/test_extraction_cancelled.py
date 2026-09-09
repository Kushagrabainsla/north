"""A cancelled task teaches north nothing.

Cancelling is the user saying the exchange does not count. Everything said inside
it goes with it - including, especially, answers to north's own clarifying
questions, which are user-authored and individually marked completed, so only the
task's ending records that they were withdrawn.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import FileContextStore
from memory.extraction import ExtractionPipeline
from utils.time import utcnow


def _pipeline(tmp_path: Path) -> ExtractionPipeline:
    return ExtractionPipeline(
        AsyncMock(), FileContextStore(tmp_path / "context"), AsyncMock(), tmp_path, fact_store=AsyncMock()
    )


def _entry(entry_id: str, *, task: str, status: LedgerStatus, text: str = "", source=LedgerSource.CLARIFICATION):
    return LedgerEntry(
        id=entry_id,
        timestamp=utcnow(),
        source=source,
        task_id=task,
        input=text,
        status=status,
    )


_ANSWER = (
    "north asked: Do you want me to submit only after you approve each application, or give "
    "standing approval for roles that match? The user answered: Standing approval for matching roles"
)


def test_answers_from_a_cancelled_task_are_dropped(tmp_path: Path) -> None:
    """The real case: a job-application run answered four questions, then was cancelled.

    Extraction ran afterwards and wrote "User gives standing approval for matching
    roles" into judgement_rules - a consent taken from a conversation the user
    walked out of, and one that widens what north may do unattended.
    """
    pipeline = _pipeline(tmp_path)
    entries = [
        _entry("a", task="task_jobs", status=LedgerStatus.COMPLETED, text=_ANSWER),
        _entry("b", task="task_jobs", status=LedgerStatus.CANCELLED, source=LedgerSource.SYSTEM),
    ]

    assert pipeline._filter_valid_entries(entries) == []


def test_a_cancellation_that_arrives_after_the_answer_still_counts(tmp_path: Path) -> None:
    """Order in the batch must not decide it - the cancellation came last in the real one."""
    pipeline = _pipeline(tmp_path)
    entries = [
        _entry("b", task="task_jobs", status=LedgerStatus.CANCELLED, source=LedgerSource.SYSTEM),
        _entry("a", task="task_jobs", status=LedgerStatus.COMPLETED, text=_ANSWER),
    ]

    assert pipeline._filter_valid_entries(entries) == []


def test_another_task_in_the_same_batch_is_untouched(tmp_path: Path) -> None:
    """One abandoned conversation must not erase what was said in a different one."""
    pipeline = _pipeline(tmp_path)
    kept = _entry(
        "c",
        task="task_other",
        status=LedgerStatus.COMPLETED,
        text="I prefer window seats on long flights",
        source=LedgerSource.PROMPT,
    )
    entries = [
        _entry("a", task="task_jobs", status=LedgerStatus.COMPLETED, text=_ANSWER),
        _entry("b", task="task_jobs", status=LedgerStatus.CANCELLED, source=LedgerSource.SYSTEM),
        kept,
    ]

    assert pipeline._filter_valid_entries(entries) == [kept]


def test_an_entry_with_no_task_is_not_swept_up(tmp_path: Path) -> None:
    """`task_id` is None for plenty of entries; None must not match a cancelled task."""
    pipeline = _pipeline(tmp_path)
    loose = LedgerEntry(
        id="d",
        timestamp=utcnow(),
        source=LedgerSource.PROMPT,
        input="I work at an infrastructure company",
        status=LedgerStatus.COMPLETED,
    )
    entries = [_entry("b", task="task_jobs", status=LedgerStatus.CANCELLED, source=LedgerSource.SYSTEM), loose]

    assert pipeline._filter_valid_entries(entries) == [loose]


@pytest.mark.parametrize("status", [LedgerStatus.FAILED, LedgerStatus.CANCELLED])
def test_a_task_that_did_not_finish_well_teaches_nothing(tmp_path: Path, status: LedgerStatus) -> None:
    pipeline = _pipeline(tmp_path)
    entry = _entry("a", task="t", status=status, text="I always deploy on Fridays", source=LedgerSource.PROMPT)
    assert pipeline._filter_valid_entries([entry]) == []
