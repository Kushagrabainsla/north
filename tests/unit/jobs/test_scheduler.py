"""Tests for jobs.scheduler — CronEntry, next_firing, next_due_entry, CronScheduler."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobs import CronEntry, CronScheduler, JobType, next_due_entry, next_firing
from jobs.sqlite_processor import SQLiteJobProcessor


# CronEntry validation


def test_cron_entry_rejects_out_of_range_hour() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=24, minute=0)


def test_cron_entry_rejects_out_of_range_minute() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=0, minute=60)


def test_cron_entry_rejects_out_of_range_weekday() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=0, minute=0, weekday=7)


# next_firing — daily (weekday=None)


def test_next_firing_daily_today_if_time_later_today() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc)  # Thursday 6am
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0)
    assert next_firing(entry, after) == datetime(2026, 5, 21, 7, 0, tzinfo=timezone.utc)


def test_next_firing_daily_tomorrow_if_time_already_passed() -> None:
    after = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)  # Thursday 9am
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0)
    assert next_firing(entry, after) == datetime(2026, 5, 22, 7, 0, tzinfo=timezone.utc)


def test_next_firing_daily_strictly_after_when_same_minute() -> None:
    """Exactly at the firing time means we go to the next day, not now."""
    after = datetime(2026, 5, 21, 7, 0, tzinfo=timezone.utc)
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0)
    assert next_firing(entry, after) == datetime(2026, 5, 22, 7, 0, tzinfo=timezone.utc)


# next_firing — weekly


def test_next_firing_weekly_advances_to_correct_weekday() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc)  # Thursday (weekday=3)
    entry = CronEntry(
        name="weekly_summary",
        agent="university",
        task="summary",
        hour=8,
        minute=0,
        weekday=0,  # Monday
    )
    # next Monday is 2026-05-25
    assert next_firing(entry, after) == datetime(2026, 5, 25, 8, 0, tzinfo=timezone.utc)


def test_next_firing_weekly_same_weekday_later_today() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc)  # Thursday
    entry = CronEntry(
        name="x", agent="a", task="t", hour=8, minute=0, weekday=3  # Thursday
    )
    assert next_firing(entry, after) == datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc)


def test_next_firing_weekly_same_weekday_already_passed_today_goes_next_week() -> None:
    after = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)  # Thursday 9am
    entry = CronEntry(
        name="x", agent="a", task="t", hour=8, minute=0, weekday=3  # Thursday 8am
    )
    assert next_firing(entry, after) == datetime(2026, 5, 28, 8, 0, tzinfo=timezone.utc)


# next_due_entry


def test_next_due_entry_returns_none_for_empty() -> None:
    assert next_due_entry([], datetime.now(timezone.utc)) is None


def test_next_due_entry_picks_earliest_firing() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc)
    early = CronEntry(name="early", agent="a", task="t", hour=7, minute=0)
    late = CronEntry(name="late", agent="a", task="t", hour=22, minute=0)
    weekly = CronEntry(
        name="weekly", agent="a", task="t", hour=5, minute=0, weekday=0
    )

    entry, firing = next_due_entry([late, early, weekly], after)
    assert entry.name == "early"
    assert firing == datetime(2026, 5, 21, 7, 0, tzinfo=timezone.utc)


# CronScheduler.build_job


def test_build_job_constructs_pending_cron_job(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    entry = CronEntry(name="meal_plan", agent="health", task="plan", hour=7, minute=0)
    scheduler = CronScheduler(processor, [entry])
    firing = datetime(2026, 5, 22, 7, 0, tzinfo=timezone.utc)

    job = scheduler.build_job(entry, firing)

    assert job.type is JobType.CRON
    assert job.agent == "health"
    assert job.task == "plan"
    assert job.scheduled_at == firing
    assert job.payload == {"cron_entry": "meal_plan"}
    assert job.job_id != ""


# CronScheduler.run — composition of pieces already tested above.
# The loop body is exactly: next_due_entry → asyncio.sleep → processor.enqueue.
# Each piece has its own test. The only run() behavior worth verifying
# separately is the empty-entries short-circuit.


async def test_scheduler_run_returns_when_no_entries(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [])

    # No infinite loop with empty entries — returns immediately.
    await scheduler.run()
