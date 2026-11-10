"""Unit tests for FailureHandler: when an agent failure is retried, and how errors are classified.

See docs/CODING_STYLE.md Sections 5.2, 6.7, 10.3, 11, 13, 14.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.failure_handler import (
    _NON_RETRYABLE_ERROR_TYPES,
    FailureHandler,
    classify_error,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(
    tmp_path: Path,
    *,
    max_retries: int = 3,
    base_cooldown_seconds: float = 0.0,
) -> FailureHandler:
    return FailureHandler(stream_manager=None, max_retries=max_retries, base_cooldown_seconds=base_cooldown_seconds)


# ---------------------------------------------------------------------------
# Retry decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_failure_retries_until_the_terminal_attempt(tmp_path: Path) -> None:
    handler = _make_handler(tmp_path, max_retries=2, base_cooldown_seconds=0.0)

    assert await handler.handle_failure("t1", "north", RuntimeError("err")) is True
    assert await handler.handle_failure("t1", "north", RuntimeError("err")) is False


@pytest.mark.asyncio
async def test_handle_failure_returns_true_before_max_retries(tmp_path: Path) -> None:
    """handle_failure must return True for every attempt strictly before max_retries."""
    handler = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)

    r1 = await handler.handle_failure("t1", "north", RuntimeError("e"))
    r2 = await handler.handle_failure("t1", "north", RuntimeError("e"))
    r3 = await handler.handle_failure("t1", "north", RuntimeError("e"))

    assert r1 is True
    assert r2 is True
    assert r3 is False  # third attempt == max_retries


# ---------------------------------------------------------------------------
# Bug 2: retry counter memory leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_count_cleared_after_terminal_failure(tmp_path: Path) -> None:
    """Retry counts must be removed from _retry_counts when retries are exhausted.

    A long-running process handling many task failures would leak memory
    indefinitely without this cleanup because _retry_counts is an unbounded dict.
    """
    handler = _make_handler(tmp_path, max_retries=2, base_cooldown_seconds=0.0)

    await handler.handle_failure("t1", "finance", RuntimeError("attempt 1"))
    await handler.handle_failure("t1", "finance", RuntimeError("attempt 2 - terminal"))

    assert ("t1", "finance") not in handler._retry_counts, (
        "Retry count was not cleared after terminal failure - memory leak"
    )


@pytest.mark.asyncio
async def test_retry_count_preserved_between_retriable_failures(tmp_path: Path) -> None:
    """Retry counts must accumulate between retriable failures."""
    handler = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)

    await handler.handle_failure("t1", "finance", RuntimeError("attempt 1"))

    assert ("t1", "finance") in handler._retry_counts, "Retry count was prematurely cleared on a retriable failure"
    assert handler._retry_counts[("t1", "finance")] == 1


@pytest.mark.asyncio
async def test_retry_count_increments_per_failure(tmp_path: Path) -> None:
    """Retry counter must increment with each failure, not reset or skip values."""
    handler = _make_handler(tmp_path, max_retries=5, base_cooldown_seconds=0.0)

    for expected in range(1, 4):
        await handler.handle_failure("t1", "search", RuntimeError("err"))
        assert handler._retry_counts[("t1", "search")] == expected, (
            f"Expected retry count={expected}, got {handler._retry_counts[('t1', 'search')]}"
        )


def test_clear_retry_count_is_idempotent(tmp_path: Path) -> None:
    """clear_retry_count must not raise when called on a key that doesn't exist."""
    handler = _make_handler(tmp_path)
    # Should not raise
    handler.clear_retry_count("nonexistent-task", "nonexistent-agent")


# ---------------------------------------------------------------------------
# B3: filesystem errors are deterministic, not retryable network failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FileNotFoundError("prompts/system.md"), "config_error"),
        (PermissionError("permission denied"), "config_error"),
        (IsADirectoryError("is a directory"), "config_error"),
        (ConnectionError("reset by peer"), "network"),
        (ConnectionRefusedError("refused"), "network"),
        (TimeoutError("timed out"), "timeout"),
        (RuntimeError("connection refused"), "network"),
        (RuntimeError("some programming bug"), "logic_error"),
    ],
)
def test_classify_error_tags(exc: Exception, expected: str) -> None:
    """Filesystem OSErrors classify as config_error, not the retryable network bucket."""
    assert classify_error(exc) == expected


@pytest.mark.asyncio
async def test_handle_failure_does_not_retry_filesystem_errors(tmp_path: Path) -> None:
    """A deterministic filesystem failure (missing file) must not be retried (B3)."""
    handler = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)

    should_retry = await handler.handle_failure("t1", "coder", FileNotFoundError("prompts/system.md"))

    assert should_retry is False


# ---------------------------------------------------------------------------
# Model scarcity: honest classification + no pointless retry
# ---------------------------------------------------------------------------


def test_classify_error_tags_model_pool_exhaustion_as_model_unavailable() -> None:
    """A fully-exhausted pool / dead quota is an availability condition, not a
    north bug - even when wrapped by an outer error (its message has no '429')."""
    from inference.exceptions import AllModelsRateLimitedError, PaymentRequiredError
    from orchestrator.exceptions import OrchestratorError

    assert classify_error(AllModelsRateLimitedError("No completion models are available")) == "model_unavailable"
    assert classify_error(PaymentRequiredError("openrouter/paid", "openrouter")) == "model_unavailable"

    try:
        try:
            raise AllModelsRateLimitedError("No models available for this request")
        except AllModelsRateLimitedError as inner:
            raise OrchestratorError("planner stage failed") from inner
    except OrchestratorError as wrapped:
        assert classify_error(wrapped) == "model_unavailable"

    # Unrelated errors are untouched.
    assert classify_error(ValueError("boom")) == "logic_error"
    assert classify_error(TimeoutError("slow")) == "timeout"


def test_model_unavailable_is_non_retryable() -> None:
    assert "model_unavailable" in _NON_RETRYABLE_ERROR_TYPES


@pytest.mark.asyncio
async def test_handle_failure_does_not_retry_model_unavailable(tmp_path: Path) -> None:
    """The dispatcher already walked the whole pool; re-running the agent just
    hits the same wall, so a model-unavailable failure must not be retried."""
    from inference.exceptions import AllModelsRateLimitedError

    handler = _make_handler(tmp_path, max_retries=3, base_cooldown_seconds=0.0)

    should_retry = await handler.handle_failure(
        "t1", "reviewer", AllModelsRateLimitedError("No completion models are available")
    )
    assert should_retry is False, "model-unavailable must be terminal on the first attempt (no retry)"
