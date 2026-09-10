"""Per-task handoff directory locations under ``NORTH_HOME``.

The handoff area (``<NORTH_HOME>/tasks``) is the single writable carve-out inside
the otherwise-blocked ``~/.north`` home: it holds the internal pipeline artifacts
agents pass to one another (research notes, specs, QA reports). This module owns
where those directories live and their lifecycle (create, prune); the
sensitive-path gate in ``tools._path`` re-exports :func:`_handoff_root` to keep
the carve-out policy anchored to the same cached root.

``NORTH_HOME`` is read from the environment (kept in sync with
``config.settings`` by env, not import, so this stays a dependency-light platform
utility). See docs/CODING_STYLE.md Section 16.1.1.
"""

from __future__ import annotations

import functools
import os
import shutil
import time
from collections.abc import Container
from pathlib import Path


@functools.lru_cache(maxsize=1)
def _handoff_root() -> str:
    """Absolute path of the per-task handoff area: ``<NORTH_HOME>/tasks``.

    Derived from the same ``NORTH_HOME`` override as ``config.settings`` (kept in
    sync by env, not import, so this module stays dependency-light). This single
    subtree is the *only* writable carve-out inside the otherwise-blocked
    ``~/.north`` home - secrets (``secret.key``), DBs, and ``.env`` live in the
    home root, outside ``tasks/``, so they remain blocked.
    """
    base = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
    try:
        return str((base / "tasks").resolve())
    except Exception:
        return str(base / "tasks")


def handoff_dir_for(task_id: str) -> str:
    """Absolute per-task handoff directory: ``<NORTH_HOME>/tasks/<task_id>``.

    The single source of truth for where agents write internal pipeline
    artifacts (research notes, specs, QA reports). Injected into agent prompts
    so paths are never hardcoded or workspace-relative.
    """
    return f"{_handoff_root()}/{task_id}"


def ensure_handoff_dir(task_id: str) -> str:
    """Create this task's handoff directory and return its path.

    Agents are handed this path in their system context as somewhere they can
    read and write, and several of them check it before doing anything. Nothing
    created it: it appeared only as a side effect of whichever component happened
    to write a file there first, so a pipeline whose *first* step reads - the
    researcher looking for prior context - found it missing and stopped the task.

    Called once when a task starts, so the promise made to every agent is true
    before any of them acts on it.
    """
    path = Path(handoff_dir_for(task_id))
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def prune_handoff_dirs(retention_days: int, *, keep: Container[str] = ()) -> int:
    """Delete handoff directories past their retention window; return how many.

    Nothing ever removed these. One directory is created per task and only
    losing best-of-N candidates were cleaned up, so they accumulated for the
    life of the install - and once the cockpit lists what is inside them, every
    task ever run shows up in the library.

    Empty directories go regardless of age: they hold nothing to read and are
    pure noise. Directories for *keep* (tasks still running) are never touched.
    ``retention_days`` of 0 keeps everything that has content.
    """
    root = Path(_handoff_root())
    if not root.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86_400
    removed = 0
    for path in root.iterdir():
        if not path.is_dir() or path.name in keep:
            continue
        contents = list(path.rglob("*"))
        empty = not any(child.is_file() for child in contents)
        # Age is the newest thing in there: an artifact written late in a long
        # task must not be aged out on the directory's creation time.
        newest = max((child.stat().st_mtime for child in contents if child.is_file()), default=0.0)
        if empty or (retention_days > 0 and newest < cutoff):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
