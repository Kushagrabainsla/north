"""Committing the coder's work without spending model turns on it.

Creating a branch, staging each changed file, and committing are steps that need
no judgement - and every one of them cost a full LLM round-trip. Measured on one
two-line fix: the coder used roughly twelve turns, six of them version control,
with three git approval cards raised back to back while the model decided "now
add", waited, "now add again", waited, "now commit". That was about a third of
its 142 seconds.

So the orchestrator does it. It reads what actually changed on disk rather than
asking the model to recall which files it touched, which is also more accurate -
the model has been known to forget one. Everything still goes through
``GitTool``, so the approval gate, the read-only argument allowlist and the
force-push block all continue to apply.
"""

from __future__ import annotations

import logging

from tools._path import PRUNED_DIRS
from tools.models import ToolInput

logger = logging.getLogger(__name__)

# Porcelain-short status codes for a path that is gone. Staging a deletion is
# right; treating the path as a file to read is not.
_DELETED = {" D", "D ", "AD"}


def changed_paths(status_short: str) -> list[str]:
    """Paths to stage, read from ``git status --short`` output.

    Untracked files count - a new test file is part of the change - but anything
    under a generated directory does not. Without that filter the first commit in
    a repo with no ``.gitignore`` sweeps in ``__pycache__``, which is exactly the
    mistake the coder prompt's "never ``git add .``" rule existed to prevent.
    """
    paths: list[str] = []
    for line in status_short.splitlines():
        if not line or line.startswith("##"):
            continue
        code, _, rest = line[:2], line[2:3], line[3:]
        path = rest.strip().strip('"')
        if not path:
            continue
        # A rename is reported as "old -> new"; the new path is what to stage.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(part in PRUNED_DIRS for part in path.split("/")):
            continue
        if code in _DELETED or path not in paths:
            paths.append(path)
    return paths


class WorkCommitter:
    """Runs the branch/stage/commit sequence through the approval-gated git tool."""

    def __init__(self, git_tool) -> None:
        self._git = git_tool

    async def _git_action(self, action: str, args: str, workspace: str, task_id: str):
        return await self._git.run(
            ToolInput(params={"action": action, "args": args, "workspace": workspace, "task_id": task_id})
        )

    async def commit(self, *, workspace: str, task_id: str, message: str) -> str | None:
        """Branch, stage what changed, and commit. Returns the branch, or None.

        None means there was nothing to commit or a step was refused - never an
        exception. A failure to commit must not fail a task whose code change was
        already applied and verified; the work is still on disk either way.
        """
        status = await self._git_action("status", "", workspace, task_id)
        if not status.success:
            logger.warning("commit: could not read git status in %s", workspace)
            return None

        paths = changed_paths(str((status.data or {}).get("stdout", "")))
        if not paths:
            return None  # the coder changed nothing; there is nothing to record

        branch = f"north/{task_id}"
        if f"## {branch}" not in str((status.data or {}).get("stdout", "")):
            checkout = await self._git_action("checkout", f"-b {branch}", workspace, task_id)
            if not checkout.success:
                # Most likely the branch exists from an earlier round - switch to it.
                checkout = await self._git_action("checkout", branch, workspace, task_id)
                if not checkout.success:
                    logger.warning("commit: could not switch to %s", branch)
                    return None

        staged = await self._git_action("add", " ".join(paths), workspace, task_id)
        if not staged.success:
            logger.warning("commit: staging refused or failed in %s", workspace)
            return None

        committed = await self._git_action("commit", message, workspace, task_id)
        if not committed.success:
            logger.warning("commit: commit refused or failed in %s", workspace)
            return None
        return branch
