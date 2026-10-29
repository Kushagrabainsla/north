"""Stopping the processes north starts, together with everything they started."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

TERMINATE_GRACE_SECONDS = 5.0


async def stop_process_group(
    process: asyncio.subprocess.Process, grace_seconds: float = TERMINATE_GRACE_SECONDS
) -> None:
    """End *process* and everything in its process group, if it is still running.

    The process must be started with ``start_new_session=True``, so it leads a
    group of its own: what a shell, a browser or a test run starts stays in that
    group, and killing only the process would leave it running. Called in a
    ``finally``, so a timeout, an error and a cancelled task all stop it.
    """
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), grace_seconds)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
