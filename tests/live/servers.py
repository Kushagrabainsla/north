"""Starting a real server in a live test, and failing by name when it cannot start."""

from __future__ import annotations

import asyncio

import pytest

STARTUP_TIMEOUT_S = 120


async def until_started(server, serving: asyncio.Task, timeout: float = STARTUP_TIMEOUT_S) -> None:
    """Wait for a uvicorn *server* run by the *serving* task to start, and fail at once when it cannot.

    When the app's startup raises, uvicorn logs the error and `serve()` returns normally. A loop that
    waited for `started` alone then waited out the test's whole time limit: from 2026-10-29 a startup
    AttributeError looked like a ten-minute hang in setup, every night (issue #82, item 6).
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not server.started:
        if serving.done():
            serving.result()  # raises what the server raised, if anything
            pytest.fail("the server stopped during startup; its error is in the log above")
        if asyncio.get_running_loop().time() > deadline:
            serving.cancel()
            pytest.fail(f"the server did not start within {timeout:.0f}s")
        await asyncio.sleep(0.05)
