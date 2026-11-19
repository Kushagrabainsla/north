"""What north learns when a task ends: a note about the task, then skills from the notes.

The notes (episodes) and skill learning ran on a timer: every 2 and every 15
minutes, then every hour as built-in flows. So a finished task was learned from
up to an hour later, and a next task in that hour did not have its note. Now the
end of a task starts both at once. The hourly flows stay, to catch what this
misses: a task failed at startup by the recovery, or a run that failed.

Ends can come close together. One run at a time: an end that arrives during a
run asks for one more run after it, not one run per end.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from utils.tasks import spawn

logger = logging.getLogger(__name__)


class _Notes(Protocol):
    async def catch_up(self) -> int: ...


class _Skills(Protocol):
    async def run_once(self) -> int: ...


class TaskEndLearning:
    """Write the notes of ended tasks, then learn skills when a new note was written."""

    def __init__(self, notes: _Notes, skills: _Skills) -> None:
        self._notes = notes
        self._skills = skills
        self._again = False
        self._running: asyncio.Task | None = None

    def task_ended(self, task_id: str) -> None:
        """A task wrote its ending row: learn from it now, or right after the run in progress."""
        self._again = True
        if self._running is None or self._running.done():
            self._running = spawn(self._learn(), name="task_end_learning", logger=logger)

    async def _learn(self) -> None:
        while self._again:
            self._again = False
            try:
                # Skills come from successful notes, so there is nothing new to learn without one.
                if await self._notes.catch_up():
                    await self._skills.run_once()
            except Exception:
                logger.exception("Learning from an ended task failed; the hourly flows will try again")
