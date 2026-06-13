"""Per-workspace mutation locks shared by every agent instance.

The agent loop serializes mutating tool calls, but that ordering used to be
per agent *instance* — a delegated coder and tester working in the same
workspace could interleave file/git mutations. These locks key on the resolved
workspace path, so any two agents mutating the same tree take the same lock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_locks: dict[str, asyncio.Lock] = {}


def workspace_lock(workspace: str) -> asyncio.Lock:
    """Return the process-wide mutation lock for *workspace*.

    An empty workspace maps to a shared default lock — unscoped mutations are
    still serialized against each other.
    """
    try:
        key = str(Path(workspace).expanduser().resolve()) if workspace else ""
    except OSError:
        key = workspace
    lock = _locks.get(key)
    if lock is None:
        lock = _locks.setdefault(key, asyncio.Lock())
    return lock
