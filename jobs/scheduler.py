"""Asyncio-native cron scheduler and v1 schedule entries. See README Section 11.3.

Implements Decision 4 (CHANGELOG): a single asyncio background task computes
the next firing across all `CronEntry` tuples, sleeps until that moment,
enqueues the matching `Job`, then recomputes. No external scheduling library.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jobs.base import JobProcessor
from jobs.models import Job, JobPriority, JobStatus, JobType
from utils.ids import generate_id
from utils.time import from_epoch, local_timezone_name, now_epoch, resolve_timezone, to_epoch

if TYPE_CHECKING:
    from jobs.cron_store import UserCronStore

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


WEEKDAYS = frozenset({0, 1, 2, 3, 4})
WEEKENDS = frozenset({5, 6})
_EVERY_DAY = frozenset(range(7))


def normalise_weekdays(value: object) -> frozenset[int] | None:
    """Coerce a stored or caller-supplied weekday selection to the canonical form.

    ``None`` means every day, and so does any selection that names all seven -
    "Mon through Sun" and "daily" are the same rule, and keeping two spellings of
    it would let ``describe()`` say "every Mon, Tue, Wed, Thu, Fri, Sat, Sun".
    A lone integer is accepted because that is what single-day schedules were
    stored as before this field held a set.
    """
    if value is None:
        return None
    days = frozenset({int(value)}) if isinstance(value, int) else frozenset(int(day) for day in value)
    if not days:
        return None
    for day in days:
        if not 0 <= day <= 6:
            raise ValueError(f"weekday must be in [0, 6], got {day}")
    return None if days == _EVERY_DAY else days


@dataclass(frozen=True)
class CronEntry:
    """One scheduled job. `weekdays` is a set of 0=Mon..6=Sun, or None for daily.

    `hour`/`minute` are wall-clock time in `tz` (an IANA name; None means the
    machine's own zone), never UTC. A recurrence is a rule, not an instant, so
    it cannot be an epoch: "07:00 in Asia/Kolkata" stays 07:00 across a DST
    shift, where a fixed epoch interval would slide to 06:00 or 08:00. Every
    *instant* the rule produces - the next firing, the job it enqueues - is an
    epoch, and is rendered in local time for the user.

    `weekdays` is a set rather than a single day because the most ordinary
    request there is - "every weekday at 9:30" - cannot be said with one day and
    is not daily either. Held as one int, that request reached the tool as a list
    and died in `int()`, so north could schedule Tuesdays but not weekdays.
    """

    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekdays: frozenset[int] | None = None
    tz: str | None = None
    enabled: bool = True
    # A short human title. `name` is the key a schedule is addressed by and
    # `task` is the prompt that runs; before this, one field was all three at
    # once, so a routine could not be given a readable title without changing
    # what it did. Empty on rows written before labels existed - `title` falls
    # back to the prompt for those.
    label: str = ""

    def __post_init__(self) -> None:
        if not (0 <= self.hour <= 23):
            raise ValueError(f"hour must be in [0, 23], got {self.hour}")
        if not (0 <= self.minute <= 59):
            raise ValueError(f"minute must be in [0, 59], got {self.minute}")
        # Frozen, so the canonical form is written back through object.__setattr__:
        # every reader downstream can then assume "None means daily" without
        # re-deriving it from a set of seven.
        object.__setattr__(self, "weekdays", normalise_weekdays(self.weekdays))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CronEntry:
        """Build an entry from a `UserCronStore` row."""
        return cls(
            name=row["name"],
            agent=row["agent"],
            task=row["task"],
            hour=row["hour"],
            minute=row["minute"],
            weekdays=row.get("weekdays"),
            tz=row.get("tz"),
            enabled=bool(row.get("enabled", True)),
            label=row.get("label") or "",
        )

    @property
    def title(self) -> str:
        """What to call this schedule in a list. The prompt, until it has a name."""
        return self.label or self.task

    @property
    def zone_name(self) -> str:
        """The IANA zone this entry's wall-clock time is read in."""
        return self.tz or local_timezone_name()

    def describe(self) -> str:
        """One line a person can check: "weekdays at 07:00 (Asia/Kolkata)"."""
        return f"{self.cadence} at {self.hour:02d}:{self.minute:02d} ({self.zone_name})"

    @property
    def cadence(self) -> str:
        """How often this runs, in the words a person would use for it."""
        if self.weekdays is None:
            return "daily"
        if self.weekdays == WEEKDAYS:
            return "weekdays"
        if self.weekdays == WEEKENDS:
            return "weekends"
        if len(self.weekdays) == 6:
            # Six days listed out is a sentence nobody reads to the end of.
            (missing,) = _EVERY_DAY - self.weekdays
            return f"every day except {WEEKDAY_NAMES[missing]}"
        return "every " + ", ".join(WEEKDAY_NAMES[day] for day in sorted(self.weekdays))


def _wall_clock(entry: CronEntry, reference: datetime) -> datetime:
    """Return `reference` moved to this entry's wall-clock hour:minute in its zone."""
    local = reference.astimezone(resolve_timezone(entry.tz))
    return local.replace(hour=entry.hour, minute=entry.minute, second=0, microsecond=0)


def next_firing(entry: CronEntry, after: datetime) -> datetime:
    """Return the first firing time strictly after `after` for `entry`.

    Pure function - no side effects, no I/O. Same `after` always yields the
    same answer, which makes the scheduler testable without mocking the clock
    in the surrounding async code.

    The arithmetic runs on the wall clock of the entry's zone, so a daily 07:00
    stays 07:00 through a DST shift rather than sliding by an hour. The returned
    datetime is aware, so callers comparing it to a UTC clock compare instants.
    """
    candidate = _wall_clock(entry, after)
    if candidate <= after:
        candidate = candidate + timedelta(days=1)
    if entry.weekdays is None:
        return candidate
    # Step a day at a time rather than computing an offset to one weekday: with a
    # set, the answer is the nearest member, and stepping is the reading of that.
    # Whole days are added to the *wall clock*, so an 07:00 rule stays 07:00 when
    # the walk crosses a DST boundary.
    for _ in range(7):
        if candidate.weekday() in entry.weekdays:
            return candidate
        candidate = candidate + timedelta(days=1)
    raise ValueError(f"{entry.name} has no runnable weekday")  # pragma: no cover - normalise_weekdays


def previous_firing(entry: CronEntry, at: datetime) -> datetime:
    """Return the most recent firing time at or before `at` for `entry`.

    The inverse of `next_firing`: used on startup to find the slot a cron should
    have run in, so a firing missed while north was down can be caught up.
    """
    candidate = _wall_clock(entry, at)
    if candidate > at:
        candidate = candidate - timedelta(days=1)
    if entry.weekdays is None:
        return candidate
    for _ in range(7):
        if candidate.weekday() in entry.weekdays:
            return candidate
        candidate = candidate - timedelta(days=1)
    raise ValueError(f"{entry.name} has no runnable weekday")  # pragma: no cover - normalise_weekdays


def next_firing_epoch(entry: CronEntry, after_epoch: float | None = None) -> float:
    """Return the entry's next firing as an epoch - the storage/display currency."""
    return to_epoch(next_firing(entry, from_epoch(after_epoch if after_epoch is not None else now_epoch())))


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
        """Every entry that should fire: built-ins, plus the user's own that are not paused.

        A paused entry is dropped here rather than skipped at firing time, so it
        never becomes the "next due" entry the loop sleeps until - otherwise the
        scheduler would wake for work it has already decided not to do.
        """
        entries = list(self._builtin_entries)
        if self._cron_store is not None:
            try:
                entries = merge_entries(entries, await self._cron_store.list())
            except Exception:
                logger.exception("CronScheduler: failed to load user cron entries")
        return [entry for entry in entries if entry.enabled]

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
        """True if this entry's slot already ran, or is still running.

        Keeps the wide catch-up window idempotent: a given scheduled slot fires at
        most once, no matter how often north restarts within the window.

        A firing that *failed* does not count, because the slot never produced
        what it was for. Asking only whether a job had been enqueued is how one
        bad morning cost a whole day: the 08:00 briefing died at the planner, and
        every restart that day looked at the failed job, called the slot spent,
        and declined to try again.
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
            and job.status is not JobStatus.FAILED
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


# V1 schedule - see docs/ARCHITECTURE.md Section 11.3.
# weekday: 0=Mon … 6=Sun, None = daily. tz is unset, so the hours below are the
# user's own local wall clock: a briefing at 08:00 means 08:00 where they live.
# Only what north needs to run itself, plus the daily briefing, ships built in -
# anything tied to one person's routine is theirs to add (`schedule_task`).
V1_CRON_ENTRIES: list[CronEntry] = [
    CronEntry(
        name="news_daily_briefing",
        label="Daily news briefing",
        agent="news_briefing",
        task=(
            "Compile the daily news briefing across Tech & AI, world events, science & health, and business & markets"
        ),
        hour=8,
        minute=0,
    ),
    CronEntry(
        name="task_context_cleanup",
        label="Nightly cleanup",
        agent="system",
        task="task_context_cleanup",
        hour=3,
        minute=0,
    ),
]


# The schedules north ships with, addressable by name so a stored row can stand
# in for one.
BUILTIN_BY_NAME: dict[str, CronEntry] = {entry.name: entry for entry in V1_CRON_ENTRIES}


def merge_entries(builtins: list[CronEntry], user_rows: list[Mapping[str, Any]]) -> list[CronEntry]:
    """The schedules that actually apply: shipped defaults, then what the user changed.

    A stored row whose name matches a built-in *replaces* it rather than sitting
    alongside it, which is what makes a built-in editable at all. Shipping a
    schedule as a constant made it unreachable: the daily briefing was created
    by asking north for it, got written into the source, and from then on could
    not be moved, paused or retimed by the person whose briefing it was.

    Deleting the stored row restores the shipped default, so an edit is always
    reversible and nothing has to be repaired by hand.
    """
    by_name = {entry.name: entry for entry in builtins}
    for row in user_rows:
        by_name[row["name"]] = CronEntry.from_row(row)
    return list(by_name.values())


def builtin_default(name: str) -> CronEntry | None:
    """The shipped form of *name*, for seeding an override or restoring one."""
    return BUILTIN_BY_NAME.get(name)
