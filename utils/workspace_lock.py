"""Per-workspace mutation locks shared by every agent instance.

Two agents changing the same tree - a delegated coder and reviewer, two
sessions in one folder - could interleave file and git changes. These locks key
on the resolved workspace path, so any two changes to the same tree take the
same lock. `Tool.execute` holds it around a change itself and nothing more: not
across the wait for an approval, and not across a run the tool starts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_locks: dict[tuple[str, asyncio.AbstractEventLoop | None], asyncio.Lock] = {}


def workspace_lock(workspace: str) -> asyncio.Lock:
    """Return the process-wide mutation lock for *workspace* in the current event loop.

    An empty workspace maps to a shared default lock - unscoped mutations are
    still serialized against each other.
    """
    try:
        key = str(Path(workspace).expanduser().resolve()) if workspace else ""
    except OSError:
        key = workspace

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    lock_key = (key, loop)
    lock = _locks.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[lock_key] = lock

    if len(_locks) > 100:
        dead = [k for k in _locks if k[1] is not None and k[1].is_closed()]
        for d in dead:
            _locks.pop(d, None)

    return lock
