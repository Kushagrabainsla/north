"""Asyncio-native cron scheduler and v1 schedule entries. See README Section 11.3.

Implements Decision 4 (CHANGELOG): a single asyncio background task computes
the next firing across all `CronEntry` tuples, sleeps until that moment,
enqueues the matching `Job`, then recomputes. No external scheduling library.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from jobs.base import JobProcessor
from jobs.models import Job, JobPriority, JobType
from utils.ids import generate_id

if TYPE_CHECKING:
    from jobs.cron_store import UserCronStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CronEntry:
    """One scheduled job. `weekday` is 0=Mon..6=Sun, or None for daily."""

    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekday: int | None = None

    def __post_init__(self) -> None:
        if not (0 <= self.hour <= 23):
            raise ValueError(f"hour must be in [0, 23], got {self.hour}")
        if not (0 <= self.minute <= 59):
            raise ValueError(f"minute must be in [0, 59], got {self.minute}")
        if self.weekday is not None and not (0 <= self.weekday <= 6):
            raise ValueError(f"weekday must be in [0, 6] or None, got {self.weekday}")


def next_firing(entry: CronEntry, after: datetime) -> datetime:
    """Return the first firing time strictly after `after` for `entry`.

    Pure function - no side effects, no I/O. Same `after` always yields the
    same answer, which makes the scheduler testable without mocking the clock
    in the surrounding async code.
    """
    candidate = after.replace(hour=entry.hour, minute=entry.minute, second=0, microsecond=0)
    if candidate <= after:
        candidate = candidate + timedelta(days=1)
    if entry.weekday is None:
        return candidate
    days_ahead = (entry.weekday - candidate.weekday()) % 7
    return candidate + timedelta(days=days_ahead)


def previous_firing(entry: CronEntry, at: datetime) -> datetime:
    """Return the most recent firing time at or before `at` for `entry`.

    The inverse of `next_firing`: used on startup to find the slot a cron should
    have run in, so a firing missed while north was down can be caught up.
    """
    candidate = at.replace(hour=entry.hour, minute=entry.minute, second=0, microsecond=0)
    if candidate > at:
        candidate = candidate - timedelta(days=1)
    if entry.weekday is None:
        return candidate
    days_back = (candidate.weekday() - entry.weekday) % 7
    return candidate - timedelta(days=days_back)


def next_due_entry(entries: list[CronEntry], after: datetime) -> tuple[CronEntry, datetime] | None:
    """Return the earliest-firing entry across `entries`, or None if empty."""
    if not entries:
        return None
    fired = [(e, next_firing(e, after)) for e in entries]
    fired.sort(key=lambda pair: pair[1])
    return fired[0]


class CronScheduler:
    """Sleep until the next scheduled firing, enqueue, repeat.

    `clock` is injectable for testing. The default uses UTC.
    Accepts an optional `cron_store` for user-defined entries that are loaded
    each iteration so newly added schedules take effect within 60 seconds.
    """

    def __init__(
        self,
        processor: JobProcessor,
        entries: list[CronEntry],
        cron_store: UserCronStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._processor = processor
        self._builtin_entries = list(entries)
        self._cron_store = cron_store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _all_entries(self) -> list[CronEntry]:
        entries = list(self._builtin_entries)
        if self._cron_store is not None:
            try:
                user = await self._cron_store.list()
                for u in user:
                    entries.append(
                        CronEntry(
                            name=u["name"],
                            agent=u["agent"],
                            task=u["task"],
                            hour=u["hour"],
                            minute=u["minute"],
                            weekday=u["weekday"],
                        )
                    )
            except Exception:
                logger.exception("CronScheduler: failed to load user cron entries")
        return entries

    def build_job(self, entry: CronEntry, scheduled_at: datetime) -> Job:
        """Construct the `Job` that will be enqueued for one firing of `entry`."""
        return Job(
            job_id=generate_id(),
            type=JobType.CRON,
            agent=entry.agent,
            task=entry.task,
            payload={"cron_entry": entry.name},
            priority=JobPriority.MEDIUM,
            scheduled_at=scheduled_at,
        )

    # On startup, catch up any firing missed while north was down within this
    # window - so e.g. the morning briefing runs when you next open north, even
    # hours late. Deduped per slot (see _already_fired) so a restart never re-runs
    # a job that already fired for its scheduled slot.
    _STARTUP_CATCHUP: timedelta = timedelta(hours=24)

    def _cannot_schedule(self) -> bool:
        return not self._builtin_entries and self._cron_store is None

    async def _already_fired(self, entry: CronEntry, slot: datetime) -> bool:
        """True if a job for this entry's slot was already enqueued (any status).

        Keeps the wide catch-up window idempotent: a given scheduled slot fires at
        most once, no matter how often north restarts within the window.
        """
        try:
            jobs = await self._processor.list_jobs(limit=200)
        except Exception:
            logger.warning("CronScheduler: could not read job history for catch-up dedup")
            return False
        return any(
            (job.payload or {}).get("cron_entry") == entry.name
            and job.scheduled_at is not None
            and job.scheduled_at >= slot
            for job in jobs
        )

    async def _catch_up(self, entries: list[CronEntry], now: datetime) -> None:
        """On startup, enqueue any firing missed while north was down (within window)."""
        window_start = now - self._STARTUP_CATCHUP
        for entry in entries:
            slot = previous_firing(entry, now)
            if slot < window_start or await self._already_fired(entry, slot):
                continue
            logger.info("CronScheduler: catching up missed firing %s (slot %s)", entry.name, slot.isoformat())
            await self._processor.enqueue(self.build_job(entry, slot))

    async def _is_already_running(self, entry: CronEntry) -> bool:
        """Return True if a prior firing of this entry is still pending or running."""
        try:
            return await self._processor.has_active_job(entry.agent, entry.task)
        except Exception:
            logger.warning("CronScheduler: could not check for running jobs, proceeding")
        return False

    async def _execute_due_entry(
        self, due: tuple[CronEntry, datetime], now: datetime, entries: list[CronEntry]
    ) -> None:
        entry, firing = due
        delay = max(0.0, (firing - now).total_seconds())
        await asyncio.sleep(min(delay, 60.0))
        now_after_sleep = self._clock()
        if firing <= now_after_sleep:
            for e in entries:
                slot = previous_firing(e, now_after_sleep)
                if slot == firing or (0 <= (now_after_sleep - slot).total_seconds() < 60):
                    if await self._already_fired(e, slot):
                        continue
                    if await self._is_already_running(e):
                        logger.info("CronScheduler: skipping %s - prior run still active", e.name)
                        continue
                    await self._processor.enqueue(self.build_job(e, slot))

    async def run(self) -> None:
        """Loop forever: pick the next due entry, sleep, enqueue, repeat.

        Sleep is capped at 60 s so user-added entries take effect within a minute.
        On the first iteration, firings missed while north was down (within
        _STARTUP_CATCHUP) are caught up - each missed slot enqueued once, deduped so
        restarts never double-fire. Returns only on cancellation.
        """
        if self._cannot_schedule():
            return
        first_tick = True
        while True:
            entries = await self._all_entries()
            if not entries:
                await asyncio.sleep(60)
                continue
            now = self._clock()
            if first_tick:
                await self._catch_up(entries, now)
                first_tick = False
            due = next_due_entry(entries, now)
            if due is None:
                await asyncio.sleep(60)
                continue
            await self._execute_due_entry(due, now, entries)


# V1 schedule - see README Section 11.3.
# weekday: 0=Mon … 6=Sun, None = daily.
V1_CRON_ENTRIES: list[CronEntry] = [
    CronEntry(
        name="news_daily_briefing",
        agent="news_briefing",
        task=(
            "Compile the daily news briefing across Tech & AI, world events, science & health, and business & markets"
        ),
        hour=8,
        minute=0,
    ),
    CronEntry(name="wellness_daily_meal_plan", agent="wellness", task="Generate today's meal plan", hour=7, minute=0),
    CronEntry(name="task_context_cleanup", agent="system", task="task_context_cleanup", hour=3, minute=0),
]
