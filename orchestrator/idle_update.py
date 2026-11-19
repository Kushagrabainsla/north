"""Update north when it is idle, so the restart cuts off no task (issue #84).

An update restarts north. A task in the middle of its work is resumed after a restart,
but one that already did something it cannot undo, such as sending a message, is failed
instead, so that it does not do it twice; a resumed task also checks its work again. On
2026-11-17 one outreach task was cut off by restarts six times and cost five times a
normal run.

"Update when idle" waits until north has been quiet for ten minutes: no task, flow run or
job running, none started in that time, and none due in the next ten minutes. A task
paused on purpose is safe across a restart and does not hold the update back; a running
task waiting on your card does, since the restart would fail it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from utils.update_record import WAITING, last_update, record_update

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 600
CHECK_SECONDS = 30


class IdleUpdate:
    """Start a waiting update once north has been idle for the whole window."""

    def __init__(
        self,
        home: Path,
        *,
        busy: Callable[[], Awaitable[str]],
        due_soon: Callable[[float], Awaitable[str]],
        last_started: Callable[[], float | None],
        start: Callable[[], None],
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._home = home
        self._busy = busy  # what is running now, in words, or ""
        self._due_soon = due_soon  # what is due within the given seconds, in words, or ""
        self._last_started = last_started  # when the last task began (clock), or None
        self._start = start
        self._window = window
        self._clock = clock
        self._quiet_since: float | None = None

    async def check(self) -> str:
        """Look once. Return why the update still waits, "started", or "" when none waits."""
        record = last_update(self._home)
        if record is None or record["state"] != "waiting":
            self._quiet_since = None
            return ""
        now = self._clock()
        if reason := await self._busy():
            self._quiet_since = now
            return self._note(f"Waiting: {reason}.")
        # Quiet since the last time something ran: seen busy, or a task started between two looks.
        # With nothing seen yet, the window starts with the first look.
        seen = [value for value in (self._quiet_since, self._last_started()) if value is not None]
        quiet_since = self._quiet_since = max(seen) if seen else now
        if reason := await self._due_soon(self._window):
            return self._note(f"Waiting: {reason}.")
        quiet = now - quiet_since
        if quiet < self._window:
            left = int((self._window - quiet + 59) // 60)
            return self._note(f"{WAITING} North is idle; it updates in about {left} min if nothing starts.")
        logger.info("North has been idle for %d s; starting the update that waited for it", int(quiet))
        self._start()
        self._quiet_since = None
        return "started"

    def _note(self, detail: str) -> str:
        """Say why it waits where the System page reads it; written only when it changes."""
        record = last_update(self._home)
        if record is not None and record["state"] == "waiting" and record.get("detail") != detail:
            record_update(self._home, "waiting", detail)
        return detail

    async def run(self, every: float = CHECK_SECONDS) -> None:
        """Look every *every* seconds, for as long as north runs."""
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not check whether north is idle for its update")
            await asyncio.sleep(every)
