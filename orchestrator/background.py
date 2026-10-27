"""North's forever-loops: restarted when one fails, and reported on /health."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_FIRST_WAIT_SECONDS = 5.0
_MAX_WAIT_SECONDS = 60.0


@dataclass
class _LoopState:
    running: bool = True
    finished: bool = False
    restarts: int = 0
    last_error: str = ""
    failed_at: float = 0.0


class BackgroundLoops:
    """Runs north's background loops, restarts one that fails, and says how each is doing.

    A loop that raised used to end for good: its guard logged the error once, and
    the scheduler, the job runner or the flow retries stayed stopped until north
    restarted, while /health still said ok. One database error was enough to stop
    every schedule (found verifying north's Oct 24 self-audit). A failed loop now
    waits, longer each time up to a minute, and starts again; a loop that ran a
    while before failing starts from the shortest wait.
    """

    def __init__(
        self,
        *,
        first_wait: float = _FIRST_WAIT_SECONDS,
        max_wait: float = _MAX_WAIT_SECONDS,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._first_wait = first_wait
        self._max_wait = max_wait
        self._sleep = sleep
        self._clock = clock
        self._states: dict[str, _LoopState] = {}

    def start(self, name: str, loop: Callable[[], Awaitable[None]]) -> asyncio.Task:
        """Run *loop* (a factory, so it can be started again) as a task kept running under *name*."""
        self._states[name] = _LoopState()
        return asyncio.create_task(self.keep_running(name, loop), name=name)

    async def keep_running(self, name: str, loop: Callable[[], Awaitable[None]]) -> None:
        """Run *loop* until it returns, starting it again whenever it fails."""
        state = self._states.setdefault(name, _LoopState())
        wait = self._first_wait
        while True:
            state.running = True
            started = self._clock()
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except (Exception, SystemExit) as exc:
                if self._clock() - started > self._max_wait:
                    wait = self._first_wait
                state.running = False
                state.restarts += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                state.failed_at = time.time()
                logger.exception("background loop %r failed; starting it again in %.0fs", name, wait)
                await self._sleep(wait)
                wait = min(wait * 2, self._max_wait)
                continue
            state.running, state.finished = False, True
            return

    def report(self) -> dict[str, Any]:
        """The /health check: failed while any loop is waiting to start again after an error."""
        loops = {}
        for name, state in sorted(self._states.items()):
            entry: dict[str, Any] = {"status": "ok" if state.running or state.finished else "failed"}
            if state.finished:
                entry["detail"] = "finished"
            if state.restarts:
                entry["restarts"] = state.restarts
                entry["last_error"] = state.last_error
            loops[name] = entry
        failing = [name for name, entry in loops.items() if entry["status"] == "failed"]
        report: dict[str, Any] = {"status": "failed" if failing else "ok", "loops": loops}
        if failing:
            report["detail"] = "starting again after an error: " + ", ".join(
                f"{name} ({loops[name]['last_error']})" for name in failing
            )
        return report
