"""Which files an agent has actually looked at before it edits them.

An exact-match edit only works when the model's copy of the text is exact, and
the reliable way to make that true is to have just read the file. Without the
precondition the model edits from memory - of a file it saw three turns and two
edits ago, or never saw at all - and the miss costs a full round-trip on a model
generating at roughly twenty tokens a second.

Scoped per task, so a fix round begun after the reviewer sends work back still
counts as having read what the first round read. Bounded, because this is a
guard rail and must never become a leak: the oldest entries are dropped once the
cap is reached.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

# Generous enough for any real task's working set, small enough to be invisible.
_MAX_ENTRIES = 2_000

_lock = threading.Lock()
_reads: OrderedDict[tuple[str, str], None] = OrderedDict()


def _key(task_id: str | None, path: str) -> tuple[str, str]:
    return (task_id or "", str(path))


def record_read(task_id: str | None, path: str) -> None:
    """Note that this task has seen *path*'s current contents."""
    with _lock:
        key = _key(task_id, path)
        _reads.pop(key, None)
        _reads[key] = None
        while len(_reads) > _MAX_ENTRIES:
            _reads.popitem(last=False)


def was_read(task_id: str | None, path: str) -> bool:
    """True when this task has read *path*.

    A call with no task id cannot be attributed to a task, so it is never
    blocked - the guard rail exists to make agents reliable, not to break the
    direct tool path or anything calling the tool outside a task.
    """
    if not task_id:
        return True
    with _lock:
        return _key(task_id, path) in _reads


def forget_task(task_id: str) -> None:
    """Drop everything recorded for a finished task."""
    with _lock:
        for key in [k for k in _reads if k[0] == task_id]:
            del _reads[key]


def reset() -> None:
    """Clear all recorded reads. For tests."""
    with _lock:
        _reads.clear()
