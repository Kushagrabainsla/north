"""SQLite-backed implementation of JobProcessor."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobs.base import JobProcessor
from jobs.exceptions import JobProcessingError
from jobs.models import Job, JobPriority, JobStatus, JobType
from utils.db import open_db_connection
from utils.time import from_epoch, now_epoch, to_epoch

logger = logging.getLogger(__name__)

# Every time column is Unix epoch seconds (UTC). Epochs sort and compare
# numerically, carry no offset to lose, and are rendered in the user's local
# zone at the edges (see utils/time.py).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_queue (
    job_id          TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    agent           TEXT NOT NULL,
    task            TEXT NOT NULL,
    payload         JSON,
    status          TEXT NOT NULL,
    priority        INTEGER NOT NULL,
    scheduled_epoch REAL NOT NULL,
    started_epoch   REAL,
    completed_epoch REAL,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 3,
    retry_epoch     REAL,
    created_epoch   REAL NOT NULL
)
"""

# The v1 table stored ISO-8601 text in these columns; each maps to the epoch
# column that replaced it. Databases created before this change are rebuilt once,
# on construction, by _migrate_iso_columns_to_epoch().
_LEGACY_TIME_COLUMNS = {
    "scheduled_at": "scheduled_epoch",
    "started_at": "started_epoch",
    "completed_at": "completed_epoch",
    "retry_after": "retry_epoch",
    "created_at": "created_epoch",
}

_TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

# How often the poll loop requeues jobs stranded in RUNNING (their worker died),
# and the lease past which a still-RUNNING job is considered abandoned. On
# startup every RUNNING job is reaped immediately (its previous owner is gone).
_REAP_INTERVAL_SECONDS: int = 300
_STALE_LEASE_SECONDS: int = 3600

# Exponential backoff before a failed job becomes eligible to retry.
_RETRY_BASE_SECONDS: int = 30
_RETRY_MAX_SECONDS: int = 3600

# Most jobs submit a task, and task submission has its own concurrency cap; this
# bounds how many jobs the processor itself holds in flight so a queue backlog
# cannot spawn unbounded concurrent work in one tick.
_MAX_CONCURRENT_JOBS: int = 8


def _retry_delay_seconds(retry_count: int) -> int:
    """Delay before a failed job retries: 30s, 60s, 120s, ... capped at 1 hour."""
    return min(_RETRY_BASE_SECONDS * (2**retry_count), _RETRY_MAX_SECONDS)


def _optional_epoch(dt: datetime | None) -> float | None:
    return to_epoch(dt) if dt is not None else None


def _optional_datetime(epoch: float | None) -> datetime | None:
    return from_epoch(epoch) if epoch is not None else None


def _legacy_text_to_epoch(text: str | None) -> float | None:
    """Convert one v1 ISO-8601 cell to epoch seconds.

    SQLite's CURRENT_TIMESTAMP default wrote naive UTC ("2026-05-21 07:00:00"),
    while Python wrote offset-aware ISO, so a naive value is read as UTC here -
    which is what it was. An unparseable cell becomes NULL rather than failing
    the migration and locking the user out of their queue.
    """
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        logger.warning("JobProcessor: dropping unparseable legacy timestamp %r", text)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _migrate_iso_columns_to_epoch(conn: sqlite3.Connection) -> None:
    """Rebuild a v1 job_queue whose time columns hold ISO-8601 text.

    Column types cannot be changed in place, so the old rows are copied into the
    epoch-shaped table and the old one dropped - once, on the first construction
    against an old database. Later constructions see no legacy column and return.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_queue)")}
    if not columns & _LEGACY_TIME_COLUMNS.keys():
        return
    logger.info("JobProcessor: migrating job_queue timestamps to epoch seconds")
    conn.execute("ALTER TABLE job_queue RENAME TO job_queue_legacy")
    conn.execute("DROP INDEX IF EXISTS idx_job_queue_pending")
    conn.execute("DROP INDEX IF EXISTS idx_job_queue_agent_task")
    conn.execute(_SCHEMA)
    rows = conn.execute("SELECT * FROM job_queue_legacy").fetchall()
    for row in rows:
        epochs = {new: _legacy_text_to_epoch(row[old]) for old, new in _LEGACY_TIME_COLUMNS.items()}
        conn.execute(
            """
            INSERT INTO job_queue (
                job_id, type, agent, task, payload, status, priority,
                scheduled_epoch, started_epoch, completed_epoch,
                retry_count, max_retries, retry_epoch, created_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["job_id"],
                row["type"],
                row["agent"],
                row["task"],
                row["payload"],
                row["status"],
                row["priority"],
                epochs["scheduled_epoch"] if epochs["scheduled_epoch"] is not None else 0.0,
                epochs["started_epoch"],
                epochs["completed_epoch"],
                row["retry_count"],
                row["max_retries"],
                epochs["retry_epoch"],
                epochs["created_epoch"] if epochs["created_epoch"] is not None else now_epoch(),
            ),
        )
    conn.execute("DROP TABLE job_queue_legacy")
    logger.info("JobProcessor: migrated %d job(s) to epoch timestamps", len(rows))


class SQLiteJobProcessor(JobProcessor):
    """Persistent job queue backed by a SQLite file.

    Schema is initialized on construction. `claim_next` uses BEGIN IMMEDIATE
    inside a single connection to atomically select-then-update, so two
    concurrent claimers can't pick up the same job.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()
        # Strong references to in-flight job tasks. The event loop only keeps
        # weak refs, so without this a dispatched job can be garbage-collected
        # mid-run and silently stay RUNNING forever (CODING_STYLE §10.5).
        self._running_jobs: set[asyncio.Task] = set()
        self._wake_event = asyncio.Event()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            _migrate_iso_columns_to_epoch(conn)
            # Indexes for the hot paths: claim_next (pending eligibility + ordering)
            # and the cron duplicate-run guard (agent + task + status).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_queue_pending ON job_queue (status, priority, scheduled_epoch)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_queue_agent_task ON job_queue (agent, task, status)")

    async def enqueue(self, job: Job) -> str:
        try:
            await asyncio.to_thread(self._enqueue_sync, job)
            self._wake_event.set()
        except sqlite3.Error as e:
            raise JobProcessingError(f"Failed to enqueue {job.job_id}: {e}") from e
        return job.job_id

    def _enqueue_sync(self, job: Job) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO job_queue (
                    job_id, type, agent, task, payload, status, priority,
                    scheduled_epoch, started_epoch, completed_epoch,
                    retry_count, max_retries, retry_epoch, created_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.type.value,
                    job.agent,
                    job.task,
                    json.dumps(job.payload),
                    job.status.value,
                    int(job.priority),
                    to_epoch(job.scheduled_at),
                    _optional_epoch(job.started_at),
                    _optional_epoch(job.completed_at),
                    job.retry_count,
                    job.max_retries,
                    _optional_epoch(job.retry_after),
                    to_epoch(job.created_at) if job.created_at else now_epoch(),
                ),
            )

    async def get(self, job_id: str) -> Job | None:
        row = await asyncio.to_thread(self._get_sync, job_id)
        return self._row_to_job(row) if row is not None else None

    def _get_sync(self, job_id: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("SELECT * FROM job_queue WHERE job_id = ?", (job_id,)).fetchone()

    async def claim_next(self) -> Job | None:
        row = await asyncio.to_thread(self._claim_next_sync)
        return self._row_to_job(row) if row is not None else None

    def _claim_next_sync(self) -> sqlite3.Row | None:
        now = now_epoch()
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM job_queue
                WHERE status = ?
                  AND scheduled_epoch <= ?
                  AND (retry_epoch IS NULL OR retry_epoch <= ?)
                ORDER BY priority ASC, scheduled_epoch ASC
                LIMIT 1
                """,
                (JobStatus.PENDING.value, now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE job_queue SET status = ?, started_epoch = ? WHERE job_id = ?",
                (JobStatus.RUNNING.value, now, row["job_id"]),
            )
            return conn.execute("SELECT * FROM job_queue WHERE job_id = ?", (row["job_id"],)).fetchone()

    async def mark_completed(self, job_id: str) -> None:
        await asyncio.to_thread(self._set_terminal_sync, job_id, JobStatus.COMPLETED)

    async def mark_failed(self, job_id: str, retry_after: datetime | None = None) -> None:
        await asyncio.to_thread(self._mark_failed_sync, job_id, retry_after)

    def _mark_failed_sync(self, job_id: str, retry_after: datetime | None) -> None:
        if retry_after is not None:
            with open_db_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM job_queue WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row and row["retry_count"] >= row["max_retries"]:
                    # Max retries exhausted - mark terminal instead of re-queuing.
                    conn.execute(
                        "UPDATE job_queue SET status = ?, completed_epoch = ? WHERE job_id = ?",
                        (JobStatus.FAILED.value, now_epoch(), job_id),
                    )
                    return
                conn.execute(
                    """
                    UPDATE job_queue
                    SET status = ?, retry_epoch = ?, retry_count = retry_count + 1
                    WHERE job_id = ?
                    """,
                    (JobStatus.PENDING.value, to_epoch(retry_after), job_id),
                )
            return
        self._set_terminal_sync(job_id, JobStatus.FAILED)

    async def cancel(self, job_id: str) -> None:
        await asyncio.to_thread(self._cancel_sync, job_id)

    def _cancel_sync(self, job_id: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE job_queue
                SET status = ?, completed_epoch = ?
                WHERE job_id = ?
                  AND status NOT IN (?, ?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    now_epoch(),
                    job_id,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )

    def _set_terminal_sync(self, job_id: str, status: JobStatus) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status; expected one of {_TERMINAL_STATUSES}")
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE job_queue SET status = ?, completed_epoch = ? WHERE job_id = ?",
                (status.value, now_epoch(), job_id),
            )

    async def has_active_job(self, agent: str, task: str) -> bool:
        return await asyncio.to_thread(self._has_active_job_sync, agent, task)

    def _has_active_job_sync(self, agent: str, task: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM job_queue WHERE agent = ? AND task = ? AND status IN (?, ?) LIMIT 1",
                (agent, task, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
            return row is not None

    async def list_jobs(self, status: JobStatus | None = None, limit: int = 100) -> list[Job]:
        rows = await asyncio.to_thread(self._list_sync, status, limit)
        return [self._row_to_job(r) for r in rows]

    async def reap_stale_running(self, lease_seconds: int) -> int:
        """Requeue jobs stuck in RUNNING past *lease_seconds* (0 = all RUNNING).

        A RUNNING job whose worker died never reaches a terminal state and, via
        has_active_job(), blocks its cron entry from ever scheduling again. Each
        reaped job goes back to PENDING with an incremented retry_count (so a job
        that keeps killing its worker is eventually FAILED, not requeued forever).
        Returns the number of jobs reaped.
        """
        return await asyncio.to_thread(self._reap_stale_running_sync, lease_seconds)

    def _reap_stale_running_sync(self, lease_seconds: int) -> int:
        now = now_epoch()
        cutoff = now - lease_seconds
        requeued = 0
        failed = 0
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT job_id, retry_count, max_retries FROM job_queue "
                "WHERE status = ? AND (started_epoch IS NULL OR started_epoch <= ?)",
                (JobStatus.RUNNING.value, cutoff),
            ).fetchall()
            for row in rows:
                if row["retry_count"] >= row["max_retries"]:
                    conn.execute(
                        "UPDATE job_queue SET status = ?, completed_epoch = ? WHERE job_id = ?",
                        (JobStatus.FAILED.value, now, row["job_id"]),
                    )
                    failed += 1
                else:
                    conn.execute(
                        "UPDATE job_queue SET status = ?, started_epoch = NULL, "
                        "retry_count = retry_count + 1 WHERE job_id = ?",
                        (JobStatus.PENDING.value, row["job_id"]),
                    )
                    requeued += 1
        if requeued or failed:
            logger.warning(
                "JobProcessor: reaped %d stale RUNNING job(s) - %d requeued, %d failed",
                requeued + failed,
                requeued,
                failed,
            )
        return requeued + failed

    async def run(
        self,
        on_job: Callable[[Job], Awaitable[Any]] | None = None,
        poll_interval_seconds: int = 5,
    ) -> None:
        """Poll the queue and dispatch claimed jobs via `on_job`."""
        # On startup every RUNNING job was abandoned by a dead worker - requeue now.
        try:
            await self.reap_stale_running(0)
        except Exception:
            logger.exception("JobProcessor: startup reap failed")
        last_reap = time.monotonic()

        while True:
            try:
                # Drain queue: claim pending jobs before sleeping, but never hold
                # more than _MAX_CONCURRENT_JOBS in flight - a large backlog would
                # otherwise spawn one task per job at once and stampede the
                # inference pool. Jobs left in the queue are claimed next tick.
                while len(self._running_jobs) < _MAX_CONCURRENT_JOBS:
                    job = await self.claim_next()
                    if job is None:
                        break
                    if on_job is not None:
                        task = asyncio.create_task(
                            self._run_job(job, on_job),
                            name=f"job-{job.job_id}",
                        )
                        self._running_jobs.add(task)
                        task.add_done_callback(self._running_jobs.discard)
                    else:
                        await self.mark_completed(job.job_id)
                if time.monotonic() - last_reap >= _REAP_INTERVAL_SECONDS:
                    last_reap = time.monotonic()
                    await self.reap_stale_running(_STALE_LEASE_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JobProcessor: error in poll loop")
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=poll_interval_seconds)
                self._wake_event.clear()
            except TimeoutError:
                pass

    async def _run_job(self, job: Job, on_job: Callable[[Job], Awaitable[Any]]) -> None:
        """Execute one job; mark completed, or schedule a retry / fail on error."""
        try:
            await on_job(job)
            await self.mark_completed(job.job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("JobProcessor: job %s failed", job.job_id)
            retry_after = datetime.now(UTC) + timedelta(seconds=_retry_delay_seconds(job.retry_count))
            await self.mark_failed(job.job_id, retry_after=retry_after)

    def _list_sync(self, status: JobStatus | None, limit: int) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            if status is None:
                sql = "SELECT * FROM job_queue ORDER BY scheduled_epoch DESC LIMIT ?"
                return list(conn.execute(sql, (limit,)).fetchall())
            sql = "SELECT * FROM job_queue WHERE status = ? ORDER BY scheduled_epoch DESC LIMIT ?"
            return list(conn.execute(sql, (status.value, limit)).fetchall())

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            type=JobType(row["type"]),
            agent=row["agent"],
            task=row["task"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            status=JobStatus(row["status"]),
            priority=JobPriority(row["priority"]),
            scheduled_at=from_epoch(row["scheduled_epoch"]),
            started_at=_optional_datetime(row["started_epoch"]),
            completed_at=_optional_datetime(row["completed_epoch"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            retry_after=_optional_datetime(row["retry_epoch"]),
            created_at=_optional_datetime(row["created_epoch"]),
        )
