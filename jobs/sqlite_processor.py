"""SQLite-backed implementation of JobProcessor."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobs.base import JobProcessor
from jobs.exceptions import JobProcessingError
from jobs.models import Job, JobPriority, JobStatus, JobType
from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_queue (
    job_id          TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    agent           TEXT NOT NULL,
    task            TEXT NOT NULL,
    payload         JSON,
    status          TEXT NOT NULL,
    priority        INTEGER NOT NULL,
    scheduled_at    DATETIME NOT NULL,
    started_at      DATETIME,
    completed_at    DATETIME,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 3,
    retry_after     DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class SQLiteJobProcessor(JobProcessor):
    """Persistent job queue backed by a SQLite file.

    Schema is initialized on construction. `claim_next` uses BEGIN IMMEDIATE
    inside a single connection to atomically select-then-update, so two
    concurrent claimers can't pick up the same job.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    async def enqueue(self, job: Job) -> str:
        try:
            await asyncio.to_thread(self._enqueue_sync, job)
        except sqlite3.Error as e:
            raise JobProcessingError(f"Failed to enqueue {job.job_id}: {e}") from e
        return job.job_id

    def _enqueue_sync(self, job: Job) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO job_queue (
                    job_id, type, agent, task, payload, status, priority,
                    scheduled_at, started_at, completed_at,
                    retry_count, max_retries, retry_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.type.value,
                    job.agent,
                    job.task,
                    json.dumps(job.payload),
                    job.status.value,
                    int(job.priority),
                    job.scheduled_at.isoformat(),
                    job.started_at.isoformat() if job.started_at else None,
                    job.completed_at.isoformat() if job.completed_at else None,
                    job.retry_count,
                    job.max_retries,
                    job.retry_after.isoformat() if job.retry_after else None,
                ),
            )

    async def get(self, job_id: str) -> Job | None:
        row = await asyncio.to_thread(self._get_sync, job_id)
        return self._row_to_job(row) if row is not None else None

    def _get_sync(self, job_id: str) -> sqlite3.Row | None:
        with open_db_connection(self._db_path) as conn:
            return conn.execute(
                "SELECT * FROM job_queue WHERE job_id = ?", (job_id,)
            ).fetchone()

    async def claim_next(self) -> Job | None:
        row = await asyncio.to_thread(self._claim_next_sync)
        return self._row_to_job(row) if row is not None else None

    def _claim_next_sync(self) -> sqlite3.Row | None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM job_queue
                    WHERE status = ?
                      AND scheduled_at <= ?
                      AND (retry_after IS NULL OR retry_after <= ?)
                    ORDER BY priority ASC, scheduled_at ASC
                    LIMIT 1
                    """,
                    (JobStatus.PENDING.value, now, now),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE job_queue SET status = ?, started_at = ? WHERE job_id = ?",
                    (JobStatus.RUNNING.value, now, row["job_id"]),
                )
                updated = conn.execute(
                    "SELECT * FROM job_queue WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                conn.execute("COMMIT")
                return updated
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def mark_completed(self, job_id: str) -> None:
        await asyncio.to_thread(self._set_terminal_sync, job_id, JobStatus.COMPLETED)

    async def mark_failed(
        self, job_id: str, retry_after: datetime | None = None
    ) -> None:
        await asyncio.to_thread(
            self._mark_failed_sync, job_id, retry_after
        )

    def _mark_failed_sync(self, job_id: str, retry_after: datetime | None) -> None:
        if retry_after is not None:
            with open_db_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM job_queue WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row and row["retry_count"] >= row["max_retries"]:
                    # Max retries exhausted — mark terminal instead of re-queuing.
                    conn.execute(
                        "UPDATE job_queue SET status = ?, completed_at = ? WHERE job_id = ?",
                        (JobStatus.FAILED.value, datetime.now(UTC).isoformat(), job_id),
                    )
                    return
                conn.execute(
                    """
                    UPDATE job_queue
                    SET status = ?, retry_after = ?, retry_count = retry_count + 1
                    WHERE job_id = ?
                    """,
                    (JobStatus.PENDING.value, retry_after.isoformat(), job_id),
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
                SET status = ?, completed_at = ?
                WHERE job_id = ?
                  AND status NOT IN (?, ?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    datetime.now(UTC).isoformat(),
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
                "UPDATE job_queue SET status = ?, completed_at = ? WHERE job_id = ?",
                (status.value, datetime.now(UTC).isoformat(), job_id),
            )

    async def list_jobs(
        self, status: JobStatus | None = None, limit: int = 100
    ) -> list[Job]:
        rows = await asyncio.to_thread(self._list_sync, status, limit)
        return [self._row_to_job(r) for r in rows]

    async def run(
        self,
        on_job: Callable[[Job], Awaitable[Any]] | None = None,
        poll_interval_seconds: int = 5,
    ) -> None:
        """Poll the queue and dispatch claimed jobs via `on_job`."""
        while True:
            try:
                job = await self.claim_next()
                if job is not None:
                    if on_job is not None:
                        asyncio.create_task(
                            self._run_job(job, on_job),
                            name=f"job-{job.job_id}",
                        )
                    else:
                        await self.mark_completed(job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JobProcessor: error in poll loop")
            await asyncio.sleep(poll_interval_seconds)

    async def _run_job(
        self, job: Job, on_job: Callable[[Job], Awaitable[Any]]
    ) -> None:
        """Execute one job; mark completed or failed based on outcome."""
        try:
            await on_job(job)
            await self.mark_completed(job.job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("JobProcessor: job %s failed", job.job_id)
            await self.mark_failed(job.job_id)

    def _list_sync(
        self, status: JobStatus | None, limit: int
    ) -> list[sqlite3.Row]:
        with open_db_connection(self._db_path) as conn:
            if status is None:
                sql = "SELECT * FROM job_queue ORDER BY scheduled_at DESC LIMIT ?"
                return list(conn.execute(sql, (limit,)).fetchall())
            sql = (
                "SELECT * FROM job_queue WHERE status = ? "
                "ORDER BY scheduled_at DESC LIMIT ?"
            )
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
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            retry_after=(
                datetime.fromisoformat(row["retry_after"])
                if row["retry_after"]
                else None
            ),
            created_at=(
                datetime.fromisoformat(row["created_at"])
                if row["created_at"]
                else None
            ),
        )
