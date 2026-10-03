"""Starting, reading from and stopping the coding agents' processes. Shared by every backend."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from coding_agents.constants import MAX_STDERR_CHARS, TERMINATE_GRACE_SECONDS


async def tail(reader: asyncio.StreamReader) -> str:
    """The last of what a process wrote to stderr; read to the end so it never blocks on a full pipe."""
    last = ""
    while chunk := await reader.read(4096):
        last = (last + chunk.decode(errors="replace"))[-MAX_STDERR_CHARS:]
    return last


async def stop(process: asyncio.subprocess.Process) -> None:
    """End the agent and everything in its process group, if it is still running."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
