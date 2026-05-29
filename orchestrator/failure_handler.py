"""Failure Handler (Stage 4).

See docs/CODING_STYLE.md Sections 5.2, 6.7, 10.3, 11, 13, 14.1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ledger import LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.exceptions import OrchestratorError
from orchestrator.task_context import TaskContextStore

if TYPE_CHECKING:
    from orchestrator.stream import EventStreamManager

logger = logging.getLogger(__name__)


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

    async def handle_failure(
        self, task_id: str, agent_name: str, exception: Exception
    ) -> bool:
        """Processes an agent failure.

        Updates the agent's task context status to failed, decides if a retry
        is allowed, and executes the cooldown delay.

        Returns:
            True if a retry should be attempted, False otherwise.
        """
        attempts = self.get_retry_count(task_id, agent_name)
        await self._task_context_store.update_agent_status(task_id, agent_name, "failed")

        # Emit failure event
        if self._stream_manager:
            await self._stream_manager.emit(
                task_id=task_id,
                event="agent_failed",
                data={
                    "agent": agent_name,
                    "error": str(exception),
                    "retry_attempt": attempts,
                    "max_retries": self.max_retries,
                    "will_retry": attempts < self.max_retries,
                },
            )

        if attempts >= self.max_retries:
            logger.error(
                f"Agent '{agent_name}' failed in task '{task_id}' and exceeded max retries ({self.max_retries})."
            )
            return False

        # Calculate exponential back-off cooldown
        cooldown = self.base_cooldown_seconds * (2**attempts)
        logger.info(
            f"Agent '{agent_name}' failed (attempt {attempts + 1}/{self.max_retries}). "
            f"Retrying in {cooldown:.2f} seconds..."
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
        self.increment_retry_count(task_id, agent_name)
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
            if (
                entry.source == LedgerSource.AGENT
                and entry.status == LedgerStatus.COMPLETED
                and entry.agent
            ):
                # Mark agent status as completed in context
                await self._task_context_store.update_agent_status(task_id, entry.agent, "completed")
                # Write back all output keys the agent completed
                if entry.agent_output:
                    for k, v in entry.agent_output.items():
                        await self._task_context_store.write(
                            task_id=task_id,
                            agent=entry.agent,
                            key=k,
                            value=v,
                            status="completed",
                        )

        if self._stream_manager:
            await self._stream_manager.emit(
                task_id=task_id,
                event="context_reconstructed",
                data={"agents": agents},
            )
