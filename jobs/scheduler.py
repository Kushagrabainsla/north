"""Asyncio-native cron scheduler. See README Section 11.3.

Implements Decision 4 (CHANGELOG): a single asyncio background task computes
the next firing across all `CronEntry` tuples, sleeps until that moment,
enqueues the matching `Job`, then recomputes. No external scheduling library.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jobs.base import JobProcessor
from jobs.models import Job, JobPriority, JobType
from utils.ids import generate_id


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
    """

    def __init__(
        self,
        processor: JobProcessor,
        entries: list[CronEntry],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._processor = processor
        self._entries = list(entries)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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

    async def run(self) -> None:
        """Loop forever: pick the next due entry, sleep, enqueue, repeat.

        Returns only on cancellation. Safe to launch as an `asyncio.create_task`
        in the Orchestrator's lifespan (docs/CODING_STYLE.md Section 10.4).
        """
        if not self._entries:
            return
        while True:
            now = self._clock()
            due = next_due_entry(self._entries, now)
            assert due is not None  # entries non-empty
            entry, firing = due
            delay = max(0.0, (firing - now).total_seconds())
            await asyncio.sleep(delay)
            await self._processor.enqueue(self.build_job(entry, firing))
