"""A background loop that fails starts again, and /health says so while it waits."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from orchestrator.api import health as api_router
from orchestrator.api_context import ApiServices, bind_services
from orchestrator.background import BackgroundLoops


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_a_loop_that_fails_starts_again_and_waits_longer_each_time() -> None:
    """One database error used to end the scheduler for good, until north restarted."""
    waits: list[float] = []
    runs = 0

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    async def scheduler() -> None:
        nonlocal runs
        runs += 1
        if runs <= 3:
            raise sqlite3.OperationalError("database is locked")

    loops = BackgroundLoops(first_wait=5, max_wait=8, sleep=no_sleep, clock=_Clock())
    await loops.keep_running("cron_scheduler", scheduler)

    assert runs == 4
    assert waits == [5, 8, 8]
    assert loops.report()["loops"]["cron_scheduler"] == {
        "status": "ok",
        "detail": "finished",
        "restarts": 3,
        "last_error": "OperationalError: database is locked",
    }


@pytest.mark.asyncio
async def test_a_loop_that_ran_a_while_before_failing_starts_from_the_shortest_wait() -> None:
    clock = _Clock()
    waits: list[float] = []
    runs = 0

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            clock.now += 600  # a long healthy stretch before this failure
        if runs <= 3:
            raise RuntimeError("blip")

    await BackgroundLoops(first_wait=5, max_wait=60, sleep=no_sleep, clock=clock).keep_running("jobs", flaky)

    assert waits == [5, 10, 5]


@pytest.mark.asyncio
async def test_health_is_degraded_while_a_loop_waits_to_start_again() -> None:
    gate = asyncio.Event()

    async def held(seconds: float) -> None:
        await gate.wait()

    async def broken() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    loops = BackgroundLoops(sleep=held)
    task = loops.start("cron_scheduler", broken)
    await asyncio.sleep(0)

    with bind_services(ApiServices(background_loops=loops)):
        payload = await api_router.health_check()

    task.cancel()
    check = payload["checks"]["background_loops"]
    assert payload["status"] == "degraded"
    assert check["status"] == "failed"
    assert "cron_scheduler (OperationalError: disk I/O error)" in check["detail"]


@pytest.mark.asyncio
async def test_stopping_north_cancels_a_loop_rather_than_restarting_it() -> None:
    async def forever() -> None:
        await asyncio.Event().wait()

    loops = BackgroundLoops()
    task = loops.start("job_processor", forever)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
