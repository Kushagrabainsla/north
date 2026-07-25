"""Failure Handler (Stage 4).

See docs/CODING_STYLE.md Sections 5.2, 6.7, 10.3, 11, 13, 14.1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from inference.exceptions import AllModelsRateLimitedError, PaymentRequiredError, ProviderAuthError
from ledger import LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.exceptions import OrchestratorError
from orchestrator.task_context import TaskContextStore

if TYPE_CHECKING:
    from orchestrator.stream import EventStreamManager

logger = logging.getLogger(__name__)

# Exceptions meaning "no model is available right now" (the whole pool is rate
# limited or a provider quota is dead) - an availability condition, not a north
# bug. The dispatcher already walked the pool before raising these.
_MODEL_UNAVAILABLE_EXCEPTIONS = (AllModelsRateLimitedError, PaymentRequiredError, ProviderAuthError)

# Error types where a retry cannot succeed: the failure is deterministic
# (missing prompt file, bad pool name) or the model pool is exhausted, so
# retrying just burns time and cost.
_NON_RETRYABLE_ERROR_TYPES = frozenset({"config_error", "model_unavailable"})


def classify_error(exc: Exception) -> str:
    """Return a stable error-type tag for a failed agent exception.

    Tags are used in LedgerEntry.error_type and the agent_failed SSE event so
    operators can filter and alert on specific failure categories without parsing
    free-form error strings.

    Categories (mutually exclusive, checked in priority order):
      model_unavailable - the whole model pool is exhausted / quota dead
      rate_limit      - provider returned HTTP 429 or equivalent
      context_overflow - model context window exceeded
      timeout         - network or asyncio timeout
      network         - connection-level failure
      parse_error     - agent returned malformed / non-JSON output
      config_error    - misconfigured agent (missing prompt, bad pool name)
      logic_error     - everything else (programming error, assertion, etc.)
    """
    # Model scarcity first: the dispatcher exhausted the pool (or hit a dead
    # quota). Walk the cause/context chain so a wrapped scarcity error - one
    # re-raised inside OrchestratorError, NorthStarConflictError, etc. - is still
    # recognised rather than falling through to logic_error.
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, _MODEL_UNAVAILABLE_EXCEPTIONS):
            return "model_unavailable"
        cur = cur.__cause__ or cur.__context__

    msg = str(exc).lower()
    name = type(exc).__name__

    if "429" in msg or "rate limit" in msg or "ratelimit" in msg or "rate_limit" in name.lower():
        return "rate_limit"
    if "context" in msg and ("length" in msg or "window" in msg or "exceed" in msg):
        return "context_overflow"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in msg:
        return "timeout"
    # Deterministic filesystem failures (missing prompt file, permission denied)
    # are config errors, not network: a retry cannot create the file or grant
    # access, so they must stay out of the retryable network bucket.
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)):
        return "config_error"
    # Connection-level failures only. Matched as ConnectionError rather than the
    # whole OSError family, which also covers the filesystem errors handled above.
    if isinstance(exc, ConnectionError) or any(
        kw in msg for kw in ("connection", "network", "unreachable", "refused", "socket")
    ):
        return "network"
    if "agentoutputparseerror" in name.lower() or "parse" in name.lower() or "json" in msg:
        return "parse_error"
    if "agentconfigerror" in name.lower() or "config" in name.lower():
        return "config_error"
    return "logic_error"


class FailureHandler:
    """Manages agent retries, back-off cooldowns, and task context reconstruction."""

    def __init__(
        self,
        ledger_writer: LedgerWriter,
        task_context_store: TaskContextStore,
        stream_manager: EventStreamManager | None = None,
        max_retries: int = 3,
        base_cooldown_seconds: float = 2.0,
    ) -> None:
        self._ledger_writer = ledger_writer
        self._task_context_store = task_context_store
        self._stream_manager = stream_manager
        self.max_retries = max_retries
        self.base_cooldown_seconds = base_cooldown_seconds
        # Maps (task_id, agent_name) -> retry attempt count (1-indexed)
        self._retry_counts: dict[tuple[str, str], int] = {}

    def get_retry_count(self, task_id: str, agent_name: str) -> int:
        """Returns the number of retries attempted for an agent in a task."""
        return self._retry_counts.get((task_id, agent_name), 0)

    def increment_retry_count(self, task_id: str, agent_name: str) -> int:
        """Increments and returns the new retry count."""
        key = (task_id, agent_name)
        self._retry_counts[key] = self._retry_counts.get(key, 0) + 1
        return self._retry_counts[key]

    def clear_retry_count(self, task_id: str, agent_name: str) -> None:
        """Remove the retry counter for a completed agent to prevent memory leaks."""
        self._retry_counts.pop((task_id, agent_name), None)

    async def handle_failure(self, task_id: str, agent_name: str, exception: Exception) -> bool:
        """Processes an agent failure.

        Updates the agent's task context status to failed, decides if a retry
        is allowed, and executes the cooldown delay.

        Returns:
            True if a retry should be attempted, False otherwise.
        """
        # Increment first so the count reflects attempts already made (1-indexed).
        # Note: max_retries bounds total *attempts* - max_retries=3 means the
        # agent runs at most 3 times (the first run plus 2 retries).
        attempt = self.increment_retry_count(task_id, agent_name)

        error_type = classify_error(exception)
        retryable = error_type not in _NON_RETRYABLE_ERROR_TYPES

        # Emit failure event
        if self._stream_manager:
            await self._stream_manager.emit(
                task_id=task_id,
                event="agent_failed",
                data={
                    "agent": agent_name,
                    "error": str(exception),
                    "error_type": error_type,
                    "retry_attempt": attempt,
                    "max_retries": self.max_retries,
                    "will_retry": retryable and attempt < self.max_retries,
                },
            )

        if not retryable:
            await self._task_context_store.update_agent_status(task_id, agent_name, "failed")
            logger.error(
                "Agent '%s' failed in task '%s' - error_type=%s is not retryable: %s",
                agent_name,
                task_id,
                error_type,
                exception,
                exc_info=True,
            )
            self.clear_retry_count(task_id, agent_name)
            return False

        if attempt >= self.max_retries:
            await self._task_context_store.update_agent_status(task_id, agent_name, "failed")
            logger.error(
                "Agent '%s' failed in task '%s' - error_type=%s, retries exhausted (%d/%d): %s",
                agent_name,
                task_id,
                error_type,
                attempt,
                self.max_retries,
                exception,
                exc_info=True,
            )
            self.clear_retry_count(task_id, agent_name)
            return False

        # Calculate exponential back-off cooldown
        cooldown = self.base_cooldown_seconds * (2 ** (attempt - 1))
        logger.warning(
            "Agent '%s' failed in task '%s' - error_type=%s, attempt %d/%d, retrying in %.2fs: %s",
            agent_name,
            task_id,
            error_type,
            attempt,
            self.max_retries,
            cooldown,
            exception,
        )

        if self._stream_manager:
            await self._stream_manager.emit(
                task_id=task_id,
                event="agent_cooldown",
                data={
                    "agent": agent_name,
                    "cooldown_seconds": cooldown,
                },
            )

        await asyncio.sleep(cooldown)
        return True

    async def reconstruct_context(self, task_id: str, agents: list[str]) -> None:
        """Reconstructs the Task Context Object from completed agent entries in the Ledger."""
        # Initialize task context state back to fresh/pending
        await self._task_context_store.initialize_task(task_id, agents)

        # Query all ledger entries for this task
        filters = LedgerFilters(task_id=task_id, limit=500)
        try:
            entries = await self._ledger_writer.query(filters)
        except Exception as e:
            raise OrchestratorError(f"Failed to query ledger for reconstruction: {e}") from e

        # Apply completed runs chronologically
        entries.sort(key=lambda e: e.timestamp)
        for entry in entries:
            if entry.source == LedgerSource.AGENT and entry.status == LedgerStatus.COMPLETED and entry.agent:
                # Mark agent status as completed in context
                await self._task_context_store.update_agent_status(task_id, entry.agent, "completed")
                # Write back all output keys the agent completed
                await self._task_context_store.write(
                    task_id=task_id,
                    agent=entry.agent,
                    key="result",
                    value=entry.agent_output if entry.agent_output is not None else {},
                    status="completed",
                )
                await self._task_context_store.write(
                    task_id=task_id,
                    agent=entry.agent,
                    key="output",
                    value=entry.output if entry.output is not None else "",
                    status="completed",
                )

        if self._stream_manager:
            await self._stream_manager.emit(
                task_id=task_id,
                event="context_reconstructed",
                data={"agents": agents},
            )
