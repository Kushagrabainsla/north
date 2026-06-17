"""Unit tests for FailureHandler.

Covers the three critical bugs fixed in failure_handler.py:

1. reconstruct_context writes 'result' and 'output' keys matching what
   _handle_agent_result writes during normal execution.
2. handle_failure does NOT set the agent context status to 'failed' on
   retriable failures - only on the terminal attempt.
3. handle_failure clears the retry counter after exhausting retries to
   prevent unbounded memory growth.

See docs/CODING_STYLE.md Sections 5.2, 6.7, 10.3, 11, 13, 14.1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.failure_handler import FailureHandler
from orchestrator.task_context import TaskContextStore
from utils.db import open_db_connection
from utils.ids import generate_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ledger_entry(
    *,
    task_id: str,
    agent: str,
    agent_output: dict | None = None,
    output: str | None = None,
    status: LedgerStatus = LedgerStatus.COMPLETED,
) -> LedgerEntry:
    return LedgerEntry(
        id=generate_id(),
        timestamp=datetime.now(UTC),
        source=LedgerSource.AGENT,
        task_id=task_id,
        agent=agent,
        action="agent_completed",
        output=output,
        agent_output=agent_output,
        status=status,
    )


def _make_handler(
    tmp_path: Path,
    *,
    max_retries: int = 3,
    base_cooldown_seconds: float = 0.0,
) -> tuple[FailureHandler, TaskContextStore, MagicMock]:
    """Return (handler, task_context_store, mock_ledger_writer)."""
    task_context_store = TaskContextStore(db_path=tmp_path / "tasks.db")

    mock_ledger = MagicMock()
    mock_ledger.query = AsyncMock(return_value=[])

    handler = FailureHandler(
        ledger_writer=mock_ledger,
        task_context_store=task_context_store,
        stream_manager=None,
        max_retries=max_retries,
        base_cooldown_seconds=base_cooldown_seconds,
    )
    return handler, task_context_store, mock_ledger


def _read_agent_status(db_path: Path, task_id: str, agent: str) -> str:
    """Read the _status row value for an agent directly from SQLite."""
    with open_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM task_state WHERE task_id=? AND agent=? AND key='_status'",
            (task_id, agent),
        ).fetchone()
    return row["status"] if row else "missing"


# ---------------------------------------------------------------------------
# Bug 1: reconstruct_context key alignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstruct_context_writes_result_and_output_keys(tmp_path: Path) -> None:
    """reconstruct_context must write 'result' and 'output' keys to TaskContextStore.

    These are the same keys _handle_agent_result writes during normal agent
    execution (orchestrator.py:764-776). Using any other keys would silently
    drop agent data when _maybe_synthesize or _execute_hierarchical_groups
    call task_context_store.get_all() to read prior outputs.
    """
    handler, store, mock_ledger = _make_handler(tmp_path)

    structured_data = {"budget": 1000, "currency": "USD"}
    human_output = "Budget is $1,000 USD."

    mock_ledger.query = AsyncMock(
        return_value=[
            _make_ledger_entry(
                task_id="t1",
                agent="finance",
                agent_output=structured_data,
                output=human_output,
            )
        ]
    )

    await handler.reconstruct_context("t1", ["finance"])

    all_data = await store.get_all("t1")
    assert "finance" in all_data, "finance agent keys not found in reconstructed context"

    agent_data = all_data["finance"]
    assert "result" in agent_data, "'result' key missing - context reconstruction key mismatch"
    assert "output" in agent_data, "'output' key missing - context reconstruction key mismatch"
    assert agent_data["result"] == structured_data
    assert agent_data["output"] == human_output


@pytest.mark.asyncio
async def test_reconstruct_context_handles_none_agent_output(tmp_path: Path) -> None:
    """reconstruct_context must not crash when agent_output is None."""
    handler, store, mock_ledger = _make_handler(tmp_path)

    mock_ledger.query = AsyncMock(
        return_value=[
            _make_ledger_entry(
                task_id="t1",
                agent="general",
                agent_output=None,
                output="Some text",
            )
        ]
    )

    await handler.reconstruct_context("t1", ["general"])

    all_data = await store.get_all("t1")
    assert all_data["general"]["result"] == {}
    assert all_data["general"]["output"] == "Some text"


@pytest.mark.asyncio
async def test_reconstruct_context_handles_none_output(tmp_path: Path) -> None:
    """reconstruct_context must not crash when output is None."""
    handler, store, mock_ledger = _make_handler(tmp_path)

    mock_ledger.query = AsyncMock(
        return_value=[
            _make_ledger_entry(
                task_id="t1",
                agent="health",
                agent_output={"calories": 2000},
                output=None,
            )
        ]
    )

    await handler.reconstruct_context("t1", ["health"])

    all_data = await store.get_all("t1")
    assert all_data["health"]["result"] == {"calories": 2000}
    assert all_data["health"]["output"] == ""


@pytest.mark.asyncio
async def test_reconstruct_context_skips_non_completed_entries(tmp_path: Path) -> None:
    """reconstruct_context must skip entries that are not COMPLETED."""
    handler, store, mock_ledger = _make_handler(tmp_path)

    mock_ledger.query = AsyncMock(
        return_value=[
            _make_ledger_entry(
                task_id="t1",
                agent="finance",
                agent_output={"data": True},
                output="Done",
                status=LedgerStatus.FAILED,  # should be skipped
            )
        ]
    )

    await handler.reconstruct_context("t1", ["finance"])

    # get_all only returns status='completed' rows and excludes _status key
    all_data = await store.get_all("t1")
    assert all_data.get("finance", {}) == {}


# ---------------------------------------------------------------------------
# Bug 2: handle_failure status update timing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_failure_does_not_mark_failed_on_retriable_attempt(
    tmp_path: Path,
) -> None:
    """Agent status must NOT be set to 'failed' on a retriable failure attempt.

    Parallel agents waiting on task context keys raise OrchestratorError
    immediately if the source agent's _status row is 'failed'. The status must
    only transition to 'failed' once max_retries is exhausted.
    """
    handler, store, _ = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["finance"])

    # First failure - attempt 1 < max_retries 3, so must be retriable
    should_retry = await handler.handle_failure("t1", "finance", RuntimeError("rate limit"))
    assert should_retry is True, "Expected retry=True on first failure"

    status = await asyncio.to_thread(_read_agent_status, tmp_path / "tasks.db", "t1", "finance")
    assert status == "pending", (
        f"Agent status was '{status}' after retriable failure - should remain 'pending' until max_retries is exhausted"
    )


@pytest.mark.asyncio
async def test_handle_failure_marks_failed_only_on_terminal_attempt(
    tmp_path: Path,
) -> None:
    """Agent status must be set to 'failed' exactly on the terminal attempt."""
    handler, store, _ = _make_handler(tmp_path, max_retries=2, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["finance"])

    # Attempt 1 - retriable
    r1 = await handler.handle_failure("t1", "finance", RuntimeError("err"))
    assert r1 is True

    # Attempt 2 - terminal (attempt == max_retries)
    r2 = await handler.handle_failure("t1", "finance", RuntimeError("err"))
    assert r2 is False

    status = await asyncio.to_thread(_read_agent_status, tmp_path / "tasks.db", "t1", "finance")
    assert status == "failed", f"Agent status was '{status}' after terminal failure - expected 'failed'"


@pytest.mark.asyncio
async def test_handle_failure_returns_true_before_max_retries(tmp_path: Path) -> None:
    """handle_failure must return True for every attempt strictly before max_retries."""
    handler, store, _ = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["router"])

    r1 = await handler.handle_failure("t1", "router", RuntimeError("e"))
    r2 = await handler.handle_failure("t1", "router", RuntimeError("e"))
    r3 = await handler.handle_failure("t1", "router", RuntimeError("e"))

    assert r1 is True
    assert r2 is True
    assert r3 is False  # third attempt == max_retries


# ---------------------------------------------------------------------------
# Bug 3: retry counter memory leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_count_cleared_after_terminal_failure(tmp_path: Path) -> None:
    """Retry counts must be removed from _retry_counts when retries are exhausted.

    A long-running process handling many task failures would leak memory
    indefinitely without this cleanup because _retry_counts is an unbounded dict.
    """
    handler, store, _ = _make_handler(tmp_path, max_retries=2, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["finance"])

    await handler.handle_failure("t1", "finance", RuntimeError("attempt 1"))
    await handler.handle_failure("t1", "finance", RuntimeError("attempt 2 - terminal"))

    assert ("t1", "finance") not in handler._retry_counts, (
        "Retry count was not cleared after terminal failure - memory leak"
    )


@pytest.mark.asyncio
async def test_retry_count_preserved_between_retriable_failures(tmp_path: Path) -> None:
    """Retry counts must accumulate between retriable failures."""
    handler, store, _ = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["finance"])

    await handler.handle_failure("t1", "finance", RuntimeError("attempt 1"))

    assert ("t1", "finance") in handler._retry_counts, "Retry count was prematurely cleared on a retriable failure"
    assert handler._retry_counts[("t1", "finance")] == 1


@pytest.mark.asyncio
async def test_retry_count_increments_per_failure(tmp_path: Path) -> None:
    """Retry counter must increment with each failure, not reset or skip values."""
    handler, store, _ = _make_handler(tmp_path, max_retries=5, base_cooldown_seconds=0.0)
    await store.initialize_task("t1", ["search"])

    for expected in range(1, 4):
        await handler.handle_failure("t1", "search", RuntimeError("err"))
        assert handler._retry_counts[("t1", "search")] == expected, (
            f"Expected retry count={expected}, got {handler._retry_counts[('t1', 'search')]}"
        )


def test_clear_retry_count_is_idempotent(tmp_path: Path) -> None:
    """clear_retry_count must not raise when called on a key that doesn't exist."""
    handler, _, _ = _make_handler(tmp_path)
    # Should not raise
    handler.clear_retry_count("nonexistent-task", "nonexistent-agent")
