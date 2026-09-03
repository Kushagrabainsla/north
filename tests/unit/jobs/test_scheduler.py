"""Tests for jobs.scheduler - CronEntry, next_firing, next_due_entry, CronScheduler.

An entry's hour/minute are wall-clock time in its `tz`, so every entry here pins
tz="UTC" to match the UTC instants the assertions are written in. Without it the
firings would land on the wall clock of whichever machine runs the suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobs import CronEntry, CronScheduler, JobType, next_due_entry, next_firing, previous_firing
from jobs.sqlite_processor import SQLiteJobProcessor

# CronEntry validation


def test_cron_entry_rejects_out_of_range_hour() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=24, minute=0, tz="UTC")


def test_cron_entry_rejects_out_of_range_minute() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=0, minute=60, tz="UTC")


def test_cron_entry_rejects_out_of_range_weekday() -> None:
    with pytest.raises(ValueError):
        CronEntry(name="x", agent="a", task="t", hour=0, minute=0, weekday=7, tz="UTC")


# next_firing - daily (weekday=None)


def test_next_firing_daily_today_if_time_later_today() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)  # Thursday 6am
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0, tz="UTC")
    assert next_firing(entry, after) == datetime(2026, 5, 21, 7, 0, tzinfo=UTC)


def test_next_firing_daily_tomorrow_if_time_already_passed() -> None:
    after = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)  # Thursday 9am
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0, tz="UTC")
    assert next_firing(entry, after) == datetime(2026, 5, 22, 7, 0, tzinfo=UTC)


def test_next_firing_daily_strictly_after_when_same_minute() -> None:
    """Exactly at the firing time means we go to the next day, not now."""
    after = datetime(2026, 5, 21, 7, 0, tzinfo=UTC)
    entry = CronEntry(name="meal", agent="health", task="plan", hour=7, minute=0, tz="UTC")
    assert next_firing(entry, after) == datetime(2026, 5, 22, 7, 0, tzinfo=UTC)


# next_firing - weekly


def test_next_firing_weekly_advances_to_correct_weekday() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)  # Thursday (weekday=3)
    entry = CronEntry(
        name="weekly_summary",
        agent="university",
        task="summary",
        hour=8,
        minute=0,
        weekday=0,  # Monday,
        tz="UTC",
    )
    # next Monday is 2026-05-25
    assert next_firing(entry, after) == datetime(2026, 5, 25, 8, 0, tzinfo=UTC)


def test_next_firing_weekly_same_weekday_later_today() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)  # Thursday
    entry = CronEntry(
        name="x",
        agent="a",
        task="t",
        hour=8,
        minute=0,
        weekday=3,  # Thursday,
        tz="UTC",
    )
    assert next_firing(entry, after) == datetime(2026, 5, 21, 8, 0, tzinfo=UTC)


def test_next_firing_weekly_same_weekday_already_passed_today_goes_next_week() -> None:
    after = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)  # Thursday 9am
    entry = CronEntry(
        name="x",
        agent="a",
        task="t",
        hour=8,
        minute=0,
        weekday=3,  # Thursday 8am,
        tz="UTC",
    )
    assert next_firing(entry, after) == datetime(2026, 5, 28, 8, 0, tzinfo=UTC)


# next_due_entry


def test_next_due_entry_returns_none_for_empty() -> None:
    assert next_due_entry([], datetime.now(UTC)) is None


def test_next_due_entry_picks_earliest_firing() -> None:
    after = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)
    early = CronEntry(name="early", agent="a", task="t", hour=7, minute=0, tz="UTC")
    late = CronEntry(name="late", agent="a", task="t", hour=22, minute=0, tz="UTC")
    weekly = CronEntry(name="weekly", agent="a", task="t", hour=5, minute=0, weekday=0, tz="UTC")

    entry, firing = next_due_entry([late, early, weekly], after)
    assert entry.name == "early"
    assert firing == datetime(2026, 5, 21, 7, 0, tzinfo=UTC)


# CronScheduler.build_job


def test_build_job_constructs_pending_cron_job(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    entry = CronEntry(name="meal_plan", agent="health", task="plan", hour=7, minute=0, tz="UTC")
    scheduler = CronScheduler(processor, [entry])
    firing = datetime(2026, 5, 22, 7, 0, tzinfo=UTC)

    job = scheduler.build_job(entry, firing)

    assert job.type is JobType.CRON
    assert job.agent == "health"
    assert job.task == "plan"
    assert job.scheduled_at == firing
    assert job.payload == {"cron_entry": "meal_plan"}
    assert job.job_id != ""


# CronScheduler.run - composition of pieces already tested above.
# The loop body is exactly: next_due_entry → asyncio.sleep → processor.enqueue.
# Each piece has its own test. The only run() behavior worth verifying
# separately is the empty-entries short-circuit.


async def test_scheduler_run_returns_when_no_entries(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [])

    # No infinite loop with empty entries - returns immediately.
    await scheduler.run()


# previous_firing - the inverse of next_firing, used for startup catch-up


def test_previous_firing_daily_returns_today_when_time_passed() -> None:
    entry = CronEntry(name="m", agent="a", task="t", hour=7, minute=0, tz="UTC")
    at = datetime(2026, 5, 21, 10, 0, tzinfo=UTC)  # 10am, after the 7am slot
    assert previous_firing(entry, at) == datetime(2026, 5, 21, 7, 0, tzinfo=UTC)


def test_previous_firing_daily_returns_yesterday_when_time_not_yet() -> None:
    entry = CronEntry(name="m", agent="a", task="t", hour=7, minute=0, tz="UTC")
    at = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)  # 6am, before the 7am slot
    assert previous_firing(entry, at) == datetime(2026, 5, 20, 7, 0, tzinfo=UTC)


# CronScheduler startup catch-up


async def test_catch_up_enqueues_missed_slot_once_and_dedups(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    now = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)  # 10am; the 8am slot was missed 2h ago
    entry = CronEntry(name="news", agent="news_briefing", task="brief", hour=8, minute=0, tz="UTC")
    scheduler = CronScheduler(processor, [entry], clock=lambda: now)

    await scheduler._catch_up([entry], now)
    news = [j for j in await processor.list_jobs() if (j.payload or {}).get("cron_entry") == "news"]
    assert len(news) == 1
    assert news[0].scheduled_at == datetime(2026, 5, 22, 8, 0, tzinfo=UTC)

    # A restart within the window must NOT re-run the same slot.
    await scheduler._catch_up([entry], now)
    news_again = [j for j in await processor.list_jobs() if (j.payload or {}).get("cron_entry") == "news"]
    assert len(news_again) == 1


async def test_catch_up_skips_slot_older_than_window(tmp_path, monkeypatch) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    now = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)  # the 8am slot is 2h ago
    entry = CronEntry(name="news", agent="a", task="t", hour=8, minute=0, tz="UTC")
    scheduler = CronScheduler(processor, [entry], clock=lambda: now)
    monkeypatch.setattr(scheduler, "_STARTUP_CATCHUP", timedelta(hours=1))  # window shorter than the 2h gap

    await scheduler._catch_up([entry], now)
    assert not [j for j in await processor.list_jobs() if (j.payload or {}).get("cron_entry") == "news"]


async def test_same_minute_multiple_entries_both_fire(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    now = datetime(2026, 5, 22, 7, 59, 50, tzinfo=UTC)  # 10s before 8:00
    e1 = CronEntry(name="job_1", agent="a1", task="t1", hour=8, minute=0, tz="UTC")
    e2 = CronEntry(name="job_2", agent="a2", task="t2", hour=8, minute=0, tz="UTC")
    scheduler = CronScheduler(processor, [e1, e2], clock=lambda: datetime(2026, 5, 22, 8, 0, 1, tzinfo=UTC))

    due = next_due_entry([e1, e2], now)
    assert due is not None
    await scheduler._execute_due_entry(due, now, [e1, e2])

    jobs = await processor.list_jobs()
    entries_fired = {j.payload.get("cron_entry") for j in jobs if j.payload}
    assert "job_1" in entries_fired
    assert "job_2" in entries_fired
    assert len(jobs) == 2
