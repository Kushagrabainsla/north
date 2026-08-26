"""Tests for SQLiteLedgerWriter - write, get, query (Section 4 of the README)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ledger import (
    LedgerEntry,
    LedgerFilters,
    LedgerSource,
    LedgerStatus,
    SQLiteLedgerWriter,
)


@pytest.fixture
def writer(tmp_path: Path) -> SQLiteLedgerWriter:
    return SQLiteLedgerWriter(tmp_path / "ledger.db")


def _entry(
    id_: str = "e1",
    *,
    source: LedgerSource = LedgerSource.PROMPT,
    timestamp: datetime | None = None,
    **kwargs: Any,
) -> LedgerEntry:
    return LedgerEntry(
        id=id_,
        timestamp=timestamp or datetime.now(UTC),
        source=source,
        **kwargs,
    )


async def test_write_then_get_round_trips_every_field(writer: SQLiteLedgerWriter) -> None:
    original = _entry(
        id_="e1",
        source=LedgerSource.AGENT,
        task_id="task-001",
        run_id="run-001",
        parent_run_id="run-parent",
        attempt=2,
        agent="finance",
        input="Plan my week",
        action="agent_completed",
        output="Plan ready",
        agent_output={"steps": ["a", "b", "c"], "confidence": 0.92},
        tools_used=["web_search", "calendar_api"],
        model_used="claude-sonnet-4",
        tokens_in=1240,
        tokens_out=380,
        cost_usd=0.0024,
        status=LedgerStatus.COMPLETED,
    )

    returned_id = await writer.write(original)
    assert returned_id == "e1"

    fetched = await writer.get("e1")
    assert fetched is not None
    assert fetched.id == original.id
    assert fetched.source is LedgerSource.AGENT
    assert fetched.task_id == "task-001"
    assert fetched.run_id == "run-001"
    assert fetched.parent_run_id == "run-parent"
    assert fetched.attempt == 2
    assert fetched.agent == "finance"
    assert fetched.agent_output == {"steps": ["a", "b", "c"], "confidence": 0.92}
    assert fetched.tools_used == ["web_search", "calendar_api"]
    assert fetched.tokens_in == 1240
    assert fetched.cost_usd == pytest.approx(0.0024)
    assert fetched.status is LedgerStatus.COMPLETED


def test_existing_ledger_migrates_agent_run_columns_before_index_creation(tmp_path: Path) -> None:
    path = tmp_path / "old-ledger.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE ledger (
                id TEXT PRIMARY KEY, timestamp DATETIME NOT NULL, source TEXT NOT NULL,
                task_id TEXT, agent TEXT, input TEXT, action TEXT, output TEXT,
                agent_output JSON, tools_used JSON, model_used TEXT, tokens_in INTEGER,
                tokens_out INTEGER, cost_usd REAL, status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    SQLiteLedgerWriter(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(ledger)")}
    assert {"run_id", "parent_run_id", "attempt"} <= columns
    assert "idx_ledger_run_id" in indexes


async def test_get_returns_none_for_missing_entry(writer: SQLiteLedgerWriter) -> None:
    assert await writer.get("does-not-exist") is None


async def test_metrics_success_rate_ignores_retried_then_succeeded(writer: SQLiteLedgerWriter) -> None:
    """A task whose agent failed once then succeeded counts as a success, not a
    failure - only the latest per-(agent, task) status matters (review finding R1#7)."""
    base = datetime.now(UTC)
    await writer.write(
        _entry(
            "r1",
            source=LedgerSource.AGENT,
            agent="coder",
            task_id="task-retry",
            action="agent_execution_failed",
            status=LedgerStatus.FAILED,
            timestamp=base,
        )
    )
    await writer.write(
        _entry(
            "r2",
            source=LedgerSource.AGENT,
            agent="coder",
            task_id="task-retry",
            action="agent_completed",
            status=LedgerStatus.COMPLETED,
            timestamp=base + timedelta(seconds=1),
        )
    )

    metrics = await writer.get_metrics(days=7)
    coder = next(a for a in metrics["by_agent"] if a["agent"] == "coder")
    assert coder["tasks"] == 1
    assert coder["success_rate"] == 1.0


async def test_metrics_success_rate_counts_genuinely_failed_task(writer: SQLiteLedgerWriter) -> None:
    """A task whose latest agent status is failed still drags the rate down."""
    await writer.write(
        _entry(
            "f1",
            source=LedgerSource.AGENT,
            agent="reviewer",
            task_id="task-broken",
            action="agent_execution_failed",
            status=LedgerStatus.FAILED,
        )
    )

    metrics = await writer.get_metrics(days=7)
    reviewer = next(a for a in metrics["by_agent"] if a["agent"] == "reviewer")
    assert reviewer["tasks"] == 1
    assert reviewer["success_rate"] == 0.0


async def test_query_filters_by_task_id(writer: SQLiteLedgerWriter) -> None:
    await writer.write(_entry("e1", task_id="task-A"))
    await writer.write(_entry("e2", task_id="task-A"))
    await writer.write(_entry("e3", task_id="task-B"))

    results = await writer.query(LedgerFilters(task_id="task-A"))

    assert {r.id for r in results} == {"e1", "e2"}


async def test_query_filters_by_run_id(writer: SQLiteLedgerWriter) -> None:
    await writer.write(_entry("e1", task_id="task-A", run_id="run-1"))
    await writer.write(_entry("e2", task_id="task-A", run_id="run-2"))

    results = await writer.query(LedgerFilters(run_id="run-1"))

    assert [r.id for r in results] == ["e1"]


async def test_query_filters_by_agent(writer: SQLiteLedgerWriter) -> None:
    await writer.write(_entry("e1", source=LedgerSource.AGENT, agent="finance"))
    await writer.write(_entry("e2", source=LedgerSource.AGENT, agent="job"))
    await writer.write(_entry("e3", source=LedgerSource.AGENT, agent="finance"))

    results = await writer.query(LedgerFilters(agent="finance"))

    assert {r.id for r in results} == {"e1", "e3"}


async def test_query_filters_by_source(writer: SQLiteLedgerWriter) -> None:
    await writer.write(_entry("e1", source=LedgerSource.AGENT, agent="finance"))
    await writer.write(_entry("e2", source=LedgerSource.SYSTEM))
    await writer.write(_entry("e3", source=LedgerSource.AGENT, agent="job"))

    results = await writer.query(LedgerFilters(source=LedgerSource.AGENT))

    assert {r.id for r in results} == {"e1", "e3"}


async def test_query_filters_by_since(writer: SQLiteLedgerWriter) -> None:
    now = datetime.now(UTC)
    await writer.write(_entry("old", timestamp=now - timedelta(hours=2)))
    await writer.write(_entry("recent", timestamp=now - timedelta(minutes=5)))
    await writer.write(_entry("newest", timestamp=now))

    results = await writer.query(LedgerFilters(since=now - timedelta(hours=1)))

    assert {r.id for r in results} == {"recent", "newest"}


async def test_query_orders_by_timestamp_descending(writer: SQLiteLedgerWriter) -> None:
    now = datetime.now(UTC)
    await writer.write(_entry("old", timestamp=now - timedelta(hours=1)))
    await writer.write(_entry("new", timestamp=now))

    results = await writer.query(LedgerFilters())

    assert [r.id for r in results] == ["new", "old"]


async def test_query_orders_by_timestamp_ascending(writer: SQLiteLedgerWriter) -> None:
    now = datetime.now(UTC)
    await writer.write(_entry("old", timestamp=now - timedelta(hours=1)))
    await writer.write(_entry("new", timestamp=now))

    results = await writer.query(LedgerFilters(order_asc=True))

    assert [r.id for r in results] == ["old", "new"]


async def test_query_respects_limit(writer: SQLiteLedgerWriter) -> None:
    for i in range(5):
        await writer.write(_entry(f"e{i}"))

    results = await writer.query(LedgerFilters(limit=3))

    assert len(results) == 3


async def test_write_is_idempotent_failure_on_duplicate_id(
    writer: SQLiteLedgerWriter,
) -> None:
    """Ledger ids are primary keys - a duplicate id surfaces as a LedgerWriteError."""
    from ledger import LedgerWriteError

    await writer.write(_entry("dup"))
    with pytest.raises(LedgerWriteError):
        await writer.write(_entry("dup"))


async def test_search_fts5_empty_or_punctuation_returns_empty(
    writer: SQLiteLedgerWriter,
) -> None:
    """Searching for punctuation only or empty string returns empty list without error."""
    await writer.write(_entry("e1", input="Hello world"))
    results = await writer.search("   ")
    assert results == []
    results = await writer.search(" ' \" () ")
    assert results == []
