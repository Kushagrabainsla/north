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

    Pure function — no side effects, no I/O. Same `after` always yields the
    same answer, which makes the scheduler testable without mocking the clock
    in the surrounding async code.
    """
    candidate = after.replace(
        hour=entry.hour, minute=entry.minute, second=0, microsecond=0
    )
    if candidate <= after:
        candidate = candidate + timedelta(days=1)
    if entry.weekday is None:
        return candidate
    days_ahead = (entry.weekday - candidate.weekday()) % 7
    return candidate + timedelta(days=days_ahead)


def next_due_entry(
    entries: list[CronEntry], after: datetime
) -> tuple[CronEntry, datetime] | None:
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

    # How far back to look on startup for missed firings (e.g. process restart
    # after a scheduled time had already passed).
    _STARTUP_CATCHUP_MINUTES: int = 15

    def _cannot_schedule(self) -> bool:
        return not self._builtin_entries and self._cron_store is None

    def _get_reference_time(self, now: datetime, first_tick: bool) -> datetime:
        if first_tick:
            return now - timedelta(minutes=self._STARTUP_CATCHUP_MINUTES)
        return now

    async def _execute_due_entry(self, due: tuple[CronEntry, datetime], now: datetime) -> None:
        entry, firing = due
        delay = max(0.0, (firing - now).total_seconds())
        await asyncio.sleep(min(delay, 60.0))
        now_after_sleep = self._clock()
        if firing <= now_after_sleep:
            await self._processor.enqueue(self.build_job(entry, firing))

    async def run(self) -> None:
        """Loop forever: pick the next due entry, sleep, enqueue, repeat.

        Sleep is capped at 60 s so user-added entries take effect within a minute.
        On the first iteration a catch-up window is applied so firings missed
        during a brief outage (≤ _STARTUP_CATCHUP_MINUTES) are enqueued
        immediately rather than skipped until the next scheduled cycle.
        Returns only on cancellation.
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
            reference = self._get_reference_time(now, first_tick)
            first_tick = False
            due = next_due_entry(entries, reference)
            if due is None:
                await asyncio.sleep(60)
                continue
            await self._execute_due_entry(due, now)


# V1 schedule — see README Section 11.3.
# weekday: 0=Mon … 6=Sun, None = daily.
V1_CRON_ENTRIES: list[CronEntry] = [
    CronEntry(name="health_daily_meal_plan",    agent="health",     task="Generate today's meal plan",                  hour=7,  minute=0),
    CronEntry(name="university_canvas_check",   agent="university", task="Check Canvas for new assignments and deadlines", hour=8,  minute=0),
    CronEntry(name="job_internship_update",     agent="job",        task="Review internship prep progress and next steps", hour=9,  minute=0),
    CronEntry(name="finance_expense_summary",   agent="finance",    task="Summarise today's expenses",                  hour=22, minute=0),
    CronEntry(name="university_weekly_summary", agent="university", task="Weekly academic summary and upcoming deadlines", hour=8,  minute=0, weekday=0),
    CronEntry(name="finance_weekly_budget",     agent="finance",    task="Weekly budget check and savings progress",     hour=18, minute=0, weekday=6),
    CronEntry(name="task_context_cleanup",      agent="system",     task="task_context_cleanup",                        hour=3,  minute=0),
]
