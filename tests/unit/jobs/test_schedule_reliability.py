"""What happens to a scheduled firing that goes wrong, and to one that is paused.

Written from a real morning. The 08:00 news briefing fired, died at the planner
because no model could serve it, and was recorded as a *completed* job - because
the dispatcher submitted the task and returned without waiting to see how it
went. Every restart that day then read the completed job, judged the slot spent,
and declined to try again. The briefing for that day never existed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobs.cron_store import UserCronStore
from jobs.models import JobStatus
from jobs.scheduler import CronEntry, CronScheduler
from jobs.sqlite_processor import SQLiteJobProcessor

MORNING = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)  # the 08:00 slot was missed 2h ago


def briefing() -> CronEntry:
    return CronEntry(name="news", agent="general", task="brief", hour=8, minute=0, tz="UTC")


async def fired_jobs(processor: SQLiteJobProcessor, name: str = "news") -> list:
    return [j for j in await processor.list_jobs() if (j.payload or {}).get("cron_entry") == name]


# ---- a failed firing is not a finished one ----


async def test_a_failed_firing_is_retried_on_the_next_startup(tmp_path) -> None:
    """The lost-briefing case, in one test.

    A slot whose job failed has not produced what the slot was for, so catch-up
    must be willing to run it again. Asking only "was a job enqueued?" is what
    wrote the day off.
    """
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [briefing()], clock=lambda: MORNING)

    await scheduler._catch_up([briefing()], MORNING)
    (job,) = await fired_jobs(processor)
    await processor.mark_failed(job.job_id)  # the planner found no model

    await scheduler._catch_up([briefing()], MORNING)
    (again,) = await fired_jobs(processor)
    assert again.status is JobStatus.PENDING


async def test_a_completed_firing_is_still_not_repeated(tmp_path) -> None:
    """The idempotence the catch-up window exists for is unchanged."""
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [briefing()], clock=lambda: MORNING)

    await scheduler._catch_up([briefing()], MORNING)
    (job,) = await fired_jobs(processor)
    await processor.mark_completed(job.job_id)

    await scheduler._catch_up([briefing()], MORNING)
    assert len(await fired_jobs(processor)) == 1


async def test_a_firing_still_running_is_not_started_a_second_time(tmp_path) -> None:
    """A restart mid-run must not put a second copy of the same work in flight."""
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [briefing()], clock=lambda: MORNING)

    await scheduler._catch_up([briefing()], MORNING)
    claimed = await processor.claim_next()
    assert claimed is not None and claimed.status is JobStatus.RUNNING

    await scheduler._catch_up([briefing()], MORNING)
    assert len(await fired_jobs(processor)) == 1


# ---- a paused schedule does not fire ----


@pytest.mark.asyncio
async def test_a_paused_schedule_is_not_offered_to_the_loop(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_stretch", "general", "stretch", 9, 0, None, enabled=False)
    scheduler = CronScheduler(processor, [], cron_store=store, clock=lambda: MORNING)

    assert await scheduler._all_entries() == []


@pytest.mark.asyncio
async def test_resuming_puts_it_back(tmp_path) -> None:
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    store = UserCronStore(tmp_path / "cron.db")
    await store.add("user_stretch", "general", "stretch", 9, 0, None, enabled=False)
    scheduler = CronScheduler(processor, [], cron_store=store, clock=lambda: MORNING)

    await store.update("user_stretch", enabled=True)
    assert [e.name for e in await scheduler._all_entries()] == ["user_stretch"]


@pytest.mark.asyncio
async def test_a_builtin_schedule_is_always_offered(tmp_path) -> None:
    """Pausing is a property of the user's own entries; built-ins have no switch."""
    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    scheduler = CronScheduler(processor, [briefing()], clock=lambda: MORNING)
    assert [e.name for e in await scheduler._all_entries()] == ["news"]


# ---- the dispatcher waits to see what happened ----
