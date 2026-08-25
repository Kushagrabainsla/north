"""Per-workspace mutation locks shared by every agent instance.

The agent loop serializes mutating tool calls, but that ordering used to be
per agent *instance* - a delegated coder and reviewer working in the same
workspace could interleave file/git mutations. These locks key on the resolved
workspace path, so any two agents mutating the same tree take the same lock.
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
