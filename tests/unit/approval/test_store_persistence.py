"""Tests for a queue that survives a restart and keeps what you have not seen.

Issues #13 and #12 together: a card that carries prepared work is meant to sit
until you look at it, which needs two things the store did not have - it has to
survive the process, and it must not be destroyed by its own task finishing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from approval.base import Notifier
from approval.batching import BatchingNotifier
from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.store import _MAX_RESOLVED, ApprovalStore
from utils.ids import generate_id


def card(**overrides) -> Card:
    defaults = {
        "id": generate_id(),
        "type": CardType.APPROVAL,
        "task_id": "task-1",
        "agent": "job",
        "title": "Application ready",
        "message": "Submit?",
    }
    return Card(**{**defaults, **overrides})


def work_card(**overrides) -> Card:
    """A card north left behind: not blocking, and owned by a source."""
    return card(
        blocking=False,
        source="job_applications",
        fields=[CardField(name="company", value="Acme")],
        **overrides,
    )


# ── Surviving a restart ──────────────────────────────────────────────────────


def test_prepared_work_is_still_there_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    left = work_card(title="Acme - Senior Python")
    ApprovalStore(db).add(left)

    reopened = ApprovalStore(db)

    restored = reopened.get(left.id)
    assert restored is not None
    assert restored.status == "pending"
    assert restored.title == "Acme - Senior Python"


def test_a_restored_card_keeps_its_fields(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    ApprovalStore(db).add(work_card(context="the original posting"))

    restored = ApprovalStore(db).pending()[0]

    assert [f.name for f in restored.fields] == ["company"]
    assert restored.context == "the original posting"


def test_a_blocking_card_is_retired_on_restart(tmp_path: Path) -> None:
    """Its waiter died with the process - answering it would wake nobody."""
    db = tmp_path / "approvals.db"
    asked = card(blocking=True)
    ApprovalStore(db).add(asked)

    reopened = ApprovalStore(db)

    assert reopened.get(asked.id).status == ApprovalDecision.TASK_ENDED
    assert reopened.pending() == []


def test_a_resolved_decision_survives_too(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    store = ApprovalStore(db)
    left = work_card()
    store.add(left)
    store.resolve(left.id, ApprovalDecision.APPROVED, values={"company": "Acme"})

    restored = ApprovalStore(db).get(left.id)

    assert restored.status == ApprovalDecision.APPROVED
    assert restored.response == {"company": "Acme"}


def test_a_store_with_no_database_still_works(tmp_path: Path) -> None:
    """Tests and embedded runs keep the old in-memory behaviour."""
    store = ApprovalStore()
    left = work_card()
    store.add(left)

    assert store.get(left.id) is left


# ── Nothing unseen is ever dropped ───────────────────────────────────────────


def test_pending_cards_are_never_evicted(tmp_path: Path) -> None:
    """The queue silently deleted unreviewed items past its cap. That was the bug."""
    store = ApprovalStore(tmp_path / "approvals.db")
    waiting = [work_card() for _ in range(20)]
    for item in waiting:
        store.add(item)
    for _ in range(_MAX_RESOLVED + 50):
        filler = card()
        store.add(filler)
        store.resolve(filler.id, ApprovalDecision.APPROVED)

    assert len(store.pending()) == len(waiting)
    assert all(store.get(item.id) is not None for item in waiting)


def test_resolved_history_is_still_capped(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    for _ in range(_MAX_RESOLVED + 25):
        done = card()
        store.add(done)
        store.resolve(done.id, ApprovalDecision.APPROVED)

    assert len([c for c in store.all(10_000) if c.status != "pending"]) <= _MAX_RESOLVED


def test_an_evicted_card_does_not_come_back_on_restart(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    store = ApprovalStore(db)
    for _ in range(_MAX_RESOLVED + 25):
        done = card()
        store.add(done)
        store.resolve(done.id, ApprovalDecision.APPROVED)

    assert len(ApprovalStore(db).all(10_000)) <= _MAX_RESOLVED


# ── Outliving the task that produced it ──────────────────────────────────────


def test_prepared_work_survives_its_task_ending() -> None:
    """The task finishing is the normal case for prepared work, not a reason to bin it."""
    store = ApprovalStore()
    left = work_card(task_id="task-1")
    store.add(left)

    cancelled = store.cancel_for_task("task-1")

    assert cancelled == []
    assert store.get(left.id).status == "pending"


def test_a_blocking_question_is_still_cancelled_with_its_task() -> None:
    store = ApprovalStore()
    asked = card(task_id="task-1", blocking=True)
    store.add(asked)

    cancelled = store.cancel_for_task("task-1")

    assert [c.id for c in cancelled] == [asked.id]
    assert store.get(asked.id).status == ApprovalDecision.TASK_ENDED


def test_a_card_with_a_source_outlives_its_task_even_if_blocking() -> None:
    store = ApprovalStore()
    owned = card(task_id="task-1", source="job_applications", blocking=True)
    store.add(owned)

    assert store.cancel_for_task("task-1") == []


def test_waiting_for_you_lists_prepared_work_oldest_first() -> None:
    store = ApprovalStore()
    first, second = work_card(), work_card()
    store.add(first)
    store.add(second)
    blocked = card(blocking=True)
    store.add(blocked)

    assert [c.id for c in store.waiting_for_you()] == [first.id, second.id]


# ── One notification for a batch ─────────────────────────────────────────────


class _Recorder(Notifier):
    def __init__(self) -> None:
        self.delivered: list[Card] = []

    async def notify(self, card: Card) -> None:
        self.delivered.append(card)


@pytest.mark.asyncio
async def test_a_blocking_card_is_delivered_at_once() -> None:
    """Something is waiting on it - holding it back would be the wrong trade."""
    recorder = _Recorder()
    notifier = BatchingNotifier(recorder, window_seconds=60)

    await notifier.notify(card(blocking=True))

    assert len(recorder.delivered) == 1


@pytest.mark.asyncio
async def test_prepared_work_is_held_back_and_then_summarised() -> None:
    recorder = _Recorder()
    notifier = BatchingNotifier(recorder, window_seconds=60)

    for n in range(8):
        await notifier.notify(work_card(title=f"Role {n}"))
    assert recorder.delivered == [], "eight applications must not be eight alerts"

    await notifier.flush()

    assert len(recorder.delivered) == 1
    assert "8 things waiting for you" in recorder.delivered[0].title


@pytest.mark.asyncio
async def test_a_single_prepared_item_is_announced_as_itself() -> None:
    """One item does not need summarising into "1 thing waiting"."""
    recorder = _Recorder()
    notifier = BatchingNotifier(recorder, window_seconds=60)

    await notifier.notify(work_card(title="Acme - Senior Python"))
    await notifier.flush()

    assert recorder.delivered[0].title == "Acme - Senior Python"


@pytest.mark.asyncio
async def test_a_long_batch_is_truncated_in_the_summary() -> None:
    recorder = _Recorder()
    notifier = BatchingNotifier(recorder, window_seconds=60)

    for n in range(14):
        await notifier.notify(work_card(title=f"Role {n}"))
    await notifier.flush()

    assert "and 4 more" in recorder.delivered[0].message


@pytest.mark.asyncio
async def test_the_batch_flushes_itself_after_the_window() -> None:
    recorder = _Recorder()
    notifier = BatchingNotifier(recorder, window_seconds=0.01)

    await notifier.notify(work_card())
    await notifier.notify(work_card())
    await asyncio.sleep(0.05)

    assert len(recorder.delivered) == 1


@pytest.mark.asyncio
async def test_flushing_an_empty_batch_delivers_nothing() -> None:
    recorder = _Recorder()

    await BatchingNotifier(recorder).flush()

    assert recorder.delivered == []
