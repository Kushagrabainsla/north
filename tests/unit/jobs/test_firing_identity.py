"""One firing is one job, and only a schedule that existed as it is now can miss one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobs.cron_store import UserCronStore
from jobs.exceptions import JobNeverStarted
from jobs.models import JobStatus
from jobs.scheduler import CronEntry, CronScheduler, broken_schedules, merge_entries
from jobs.sqlite_processor import SQLiteJobProcessor
from utils.db import open_db_connection
from utils.time import to_epoch

MORNING = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
SLOT = datetime(2026, 5, 22, 8, 0, tzinfo=UTC)


def _entry(name: str = "news", **fields) -> CronEntry:
    return CronEntry(name=name, agent="general", task="brief", hour=8, minute=0, tz="UTC", **fields)


def _setup(tmp_path, *entries: CronEntry) -> tuple[SQLiteJobProcessor, CronScheduler]:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    return processor, CronScheduler(processor, list(entries), clock=lambda: MORNING)


# ---- one firing, one job ----


async def test_firing_a_slot_twice_is_one_job(tmp_path) -> None:
    processor, scheduler = _setup(tmp_path, _entry())

    await processor.enqueue(scheduler.build_job(_entry(), SLOT))
    await processor.enqueue(scheduler.build_job(_entry(), SLOT))

    assert len(await processor.list_jobs()) == 1


async def test_a_failed_firing_can_fire_again(tmp_path) -> None:
    processor, scheduler = _setup(tmp_path, _entry())
    job = scheduler.build_job(_entry(), SLOT)
    await processor.enqueue(job)
    await processor.mark_failed(job.job_id)

    await processor.enqueue(job)

    (row,) = await processor.list_jobs()
    assert row.status is JobStatus.PENDING


async def test_a_finished_firing_stays_finished(tmp_path) -> None:
    processor, scheduler = _setup(tmp_path, _entry())
    job = scheduler.build_job(_entry(), SLOT)
    await processor.enqueue(job)
    await processor.mark_completed(job.job_id)

    await processor.enqueue(job)

    (row,) = await processor.list_jobs()
    assert row.status is JobStatus.COMPLETED


async def test_a_job_that_could_not_start_is_failed_so_it_can_fire_again(tmp_path) -> None:
    """Recorded as completed, its slot counted as spent and the day's run was lost."""
    processor, scheduler = _setup(tmp_path, _entry())
    await processor.enqueue(scheduler.build_job(_entry(), datetime.now(UTC) - timedelta(minutes=1)))
    claimed = await processor.claim_next()

    async def never_starts(_job) -> None:
        raise JobNeverStarted("the flow is gone")

    await processor._run_job(claimed, never_starts)

    (row,) = await processor.list_jobs()
    assert row.status is JobStatus.FAILED


# ---- what counts as already running ----


async def test_a_firing_waiting_for_next_week_is_not_running(tmp_path) -> None:
    processor, scheduler = _setup(tmp_path, _entry())

    await processor.enqueue(scheduler.build_job(_entry(), datetime.now(UTC) + timedelta(days=7)))
    assert not await processor.has_active_firing("news")

    await processor.enqueue(scheduler.build_job(_entry(), datetime.now(UTC) - timedelta(minutes=1)))
    assert await processor.has_active_firing("news")


async def test_two_schedules_with_the_same_work_do_not_block_each_other(tmp_path) -> None:
    processor, scheduler = _setup(tmp_path, _entry("a"), _entry("b"))

    await processor.enqueue(scheduler.build_job(_entry("a"), datetime.now(UTC) - timedelta(minutes=1)))

    assert await processor.has_active_firing("a")
    assert not await processor.has_active_firing("b")


# ---- a slot is only missed by the schedule as it is now ----


async def test_a_slot_from_before_a_retime_is_not_caught_up(tmp_path) -> None:
    """A 09:00 moved to 10:30 ran at the next start, as if north had been off."""
    long_ago = to_epoch(MORNING - timedelta(days=30))
    retimed = _entry(created_epoch=long_ago, effective_epoch=to_epoch(MORNING - timedelta(hours=1)))
    processor, scheduler = _setup(tmp_path, retimed)

    await scheduler._catch_up([retimed], MORNING)

    assert await processor.list_jobs() == []


async def test_a_slot_since_the_last_change_is_still_caught_up(tmp_path) -> None:
    long_ago = to_epoch(MORNING - timedelta(days=30))
    entry = _entry(created_epoch=long_ago, effective_epoch=to_epoch(MORNING - timedelta(days=5)))
    processor, scheduler = _setup(tmp_path, entry)

    await scheduler._catch_up([entry], MORNING)

    assert len(await processor.list_jobs()) == 1


async def test_only_changing_when_it_runs_moves_the_schedule(tmp_path) -> None:
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_a", "general", "stretch", 9, 0, None)
    assert (await store.get("user_a"))["effective_epoch"] is None

    await store.update("user_a", label="Stretch")
    assert (await store.get("user_a"))["effective_epoch"] is None

    await store.update("user_a", hour=10)
    moved = (await store.get("user_a"))["effective_epoch"]
    assert moved is not None

    await store.update("user_a", enabled=False)
    await store.update("user_a", enabled=True)
    assert (await store.get("user_a"))["effective_epoch"] >= moved


# ---- the Settings zone ----


async def test_a_schedule_made_without_a_zone_follows_settings(tmp_path, monkeypatch) -> None:
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_a", "general", "stretch", 9, 0, None)
    row = await store.get("user_a")
    assert row["tz"] is None

    monkeypatch.setattr("jobs.scheduler.local_timezone_name", lambda: "Asia/Tokyo")
    assert CronEntry.from_row(row).zone_name == "Asia/Tokyo"


async def test_a_zone_change_means_earlier_slots_were_not_missed(tmp_path) -> None:
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_a", "general", "stretch", 9, 0, None)

    await store.zone_changed()

    assert (await store.get("user_a"))["effective_epoch"] is not None


async def test_old_schedules_start_following_settings_once(tmp_path) -> None:
    """Every stored zone was only the Settings zone on the day it was made."""
    path = tmp_path / "cron.db"
    store = UserCronStore(path)
    await store.add("user_a", "general", "stretch", 9, 0, None)
    with open_db_connection(path) as conn:
        conn.execute("ALTER TABLE user_cron_entries DROP COLUMN effective_epoch")
        conn.execute("UPDATE user_cron_entries SET tz = 'Asia/Tokyo'")

    upgraded = UserCronStore(path)
    assert (await upgraded.get("user_a"))["tz"] is None

    await upgraded.update("user_a", tz="Europe/Paris")
    assert (await UserCronStore(path).get("user_a"))["tz"] == "Europe/Paris", "only once"


# ---- a bad row ----


async def test_one_unreadable_schedule_does_not_stop_the_others_and_is_named(tmp_path) -> None:
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_good", "general", "stretch", 9, 0, None)
    await store.add("user_bad", "general", "stretch", 9, 0, None)
    rows = [{**row, "hour": 99} if row["name"] == "user_bad" else row for row in await store.list()]

    assert [entry.name for entry in merge_entries([], rows)] == ["user_good"]
    assert broken_schedules(rows) == ["user_bad"]
