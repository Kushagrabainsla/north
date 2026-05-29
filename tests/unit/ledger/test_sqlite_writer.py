"""Tests for SQLiteLedgerWriter — write, get, query (Section 4 of the README)."""

from __future__ import annotations

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
    assert fetched.agent == "finance"
    assert fetched.agent_output == {"steps": ["a", "b", "c"], "confidence": 0.92}
    assert fetched.tools_used == ["web_search", "calendar_api"]
    assert fetched.tokens_in == 1240
    assert fetched.cost_usd == pytest.approx(0.0024)
    assert fetched.status is LedgerStatus.COMPLETED


async def test_get_returns_none_for_missing_entry(writer: SQLiteLedgerWriter) -> None:
    assert await writer.get("does-not-exist") is None


async def test_query_filters_by_task_id(writer: SQLiteLedgerWriter) -> None:
    await writer.write(_entry("e1", task_id="task-A"))
    await writer.write(_entry("e2", task_id="task-A"))
    await writer.write(_entry("e3", task_id="task-B"))

    results = await writer.query(LedgerFilters(task_id="task-A"))

    assert {r.id for r in results} == {"e1", "e2"}


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


async def test_query_respects_limit(writer: SQLiteLedgerWriter) -> None:
    for i in range(5):
        await writer.write(_entry(f"e{i}"))

    results = await writer.query(LedgerFilters(limit=3))

    assert len(results) == 3


async def test_write_is_idempotent_failure_on_duplicate_id(
    writer: SQLiteLedgerWriter,
) -> None:
    """Ledger ids are primary keys — a duplicate id surfaces as a LedgerWriteError."""
    from ledger import LedgerWriteError

    await writer.write(_entry("dup"))
    with pytest.raises(LedgerWriteError):
        await writer.write(_entry("dup"))
