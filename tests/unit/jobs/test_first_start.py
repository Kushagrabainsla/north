"""A first start catches up nothing from before north was installed.

On 2026-11-22 a fresh north ran every built-in flow at its first start, each labelled
"16 h 24 min late: north was not running". A built-in ships with north, so it had no
creation time, and catch-up counted it as there at every past slot. It now exists from
when this north first had it, or from its earliest firing in the queue on an upgraded north.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobs.cron_store import UserCronStore
from jobs.scheduler import CronEntry, CronScheduler
from jobs.sqlite_processor import SQLiteJobProcessor

FIRST_START = datetime(2026, 11, 22, 16, 28, tzinfo=UTC)  # the 03:00 and 07:00 slots passed before it


def nightly() -> CronEntry:
    return CronEntry(name="nightly-cleanup", agent="system", task="clean", hour=3, minute=0, tz="UTC")


async def _start(tmp_path, at: datetime) -> tuple[SQLiteJobProcessor, CronScheduler]:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [nightly()], cron_store=UserCronStore(tmp_path / "cron.db"), clock=lambda: at)
    await scheduler._catch_up(await scheduler._all_entries(), at)
    return processor, scheduler


async def _fired(processor: SQLiteJobProcessor) -> list:
    return [job for job in await processor.list_jobs() if (job.payload or {}).get("cron_entry") == "nightly-cleanup"]


async def test_a_fresh_install_catches_up_nothing_from_before_it_existed(tmp_path) -> None:
    processor, _ = await _start(tmp_path, FIRST_START)

    assert await _fired(processor) == []


async def test_a_fresh_install_catches_up_a_slot_it_missed_after_its_first_start(tmp_path) -> None:
    await _start(tmp_path, FIRST_START)  # records when this north first had the schedule

    processor, _ = await _start(tmp_path, FIRST_START + timedelta(days=1))  # off from 03:00 to 16:28

    [job] = await _fired(processor)
    assert job.scheduled_at == datetime(2026, 11, 23, 3, 0, tzinfo=UTC)


async def test_an_upgraded_north_keeps_its_catch_up(tmp_path) -> None:
    """A built-in that fired here before existed before this start, so its missed slot still runs."""
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    earlier = CronScheduler(processor, [nightly()])
    yesterday = earlier.build_job(nightly(), datetime(2026, 11, 21, 3, 0, tzinfo=UTC))
    await processor.enqueue(yesterday)
    await processor.mark_completed(yesterday.job_id)

    processor, _ = await _start(tmp_path, FIRST_START)

    missed = [job for job in await _fired(processor) if job.job_id != yesterday.job_id]
    assert [job.scheduled_at for job in missed] == [datetime(2026, 11, 22, 3, 0, tzinfo=UTC)]
