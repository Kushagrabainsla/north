"""Stopping the processes north starts, together with everything they started."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time

TERMINATE_GRACE_SECONDS = 5.0
_POLL_SECONDS = 0.05
_COLLECT_SECONDS = 5.0


def _finished(process: asyncio.subprocess.Process) -> bool:
    """Whether *process* exited and its output ended, so nothing still running is tied to it.

    A command can start a server on purpose and send its output elsewhere: that stays. A child
    still holding the output open is what the caller gave up waiting for, and has to stop too.
    """
    if process.returncode is None:
        return False
    return all(stream is None or stream.at_eof() for stream in (process.stdout, process.stderr))


def _signal(group: int, signal_number: int) -> bool:
    """Send a signal to *group*; False when nothing in it is left to receive one.

    macOS answers "not permitted", not "no such process", for a group whose members have
    all exited but are not collected yet. Linux CI never sees that answer.
    """
    try:
        os.killpg(group, signal_number)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _end_group(group: int, grace_seconds: float) -> None:
    """Ask *group* to stop, wait out the grace period, then force it. Stops at the first sign it is gone."""
    if not _signal(group, signal.SIGTERM):
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
        if not _signal(group, 0):  # an emptied group's id can be reused: never signal it again
            return
    _signal(group, signal.SIGKILL)


async def stop_process_group(
    process: asyncio.subprocess.Process, grace_seconds: float = TERMINATE_GRACE_SECONDS
) -> None:
    """End *process* and everything in its process group, unless it already finished.

    The process must be started with ``start_new_session=True``, so it leads a
    group of its own: what a shell, a browser or a test run starts stays in that
    group, and killing only the process would leave it running. Called in a
    ``finally``, so a timeout, an error and a cancelled task all stop it. It never
    replaces the error that brought the caller here.
    """
    if _finished(process):
        return
    group = process.pid
    try:
        await _end_group(group, grace_seconds)
    except BaseException:  # a second cancel during the grace period must not leave the group running
        _signal(group, signal.SIGKILL)
        raise
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), _COLLECT_SECONDS)
