"""Git worktree isolation for mutating agent runs.

Lets an agent (e.g. the coder) work in a dedicated linked worktree on a throwaway
branch so concurrent runs never share a working tree. When the agent finishes,
its changes are *applied back* onto the base working tree under the shared
workspace lock; if they overlap another run's changes the apply is refused and
the branch is kept for manual merge instead of clobbering anything.

Only the brief apply-back touches the base tree, so isolated runs otherwise
proceed fully in parallel. Everything here is plain git plumbing over
``asyncio`` subprocesses - no third-party dependency.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
# Commit identity + no signing, injected per-call so the manager never depends on
# the repo (or user) having a configured git identity.
_COMMIT_CONFIG = (
    "-c",
    "user.name=north",
    "-c",
    "user.email=north@localhost",
    "-c",
    "commit.gpgsign=false",
)


class WorktreeError(Exception):
    """A worktree could not be created or a required git operation failed hard."""


@dataclass(frozen=True)
class Worktree:
    """A live linked worktree checked out on a throwaway branch at ``base_sha``."""

    base: str
    path: str
    branch: str
    base_sha: str


@dataclass(frozen=True)
class IntegrationResult:
    """Outcome of applying a worktree's changes back to the base working tree."""

    applied: bool  # changes landed in the base working tree
    changed: bool  # the agent produced any changes at all
    conflicted: bool  # apply refused due to overlap; branch retained for manual merge
    branch: str
    path: str


async def _run_git(
    args: list[str],
    cwd: str | Path,
    *,
    timeout: int = _GIT_TIMEOUT,
    input_data: bytes | None = None,
    binary: bool = False,
) -> tuple[int, Any, str]:
    """Run ``git <args>`` in *cwd*, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=input_data), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorktreeError(f"git {args[0] if args else ''} timed out after {timeout}s") from exc
    stdout = out if binary else out.decode(errors="replace")
    return proc.returncode, stdout, err.decode(errors="replace")


def _slug(label: str) -> str:
    """Filesystem/branch-safe short slug derived from *label*."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in label).strip("-")
    return (safe or "agent")[:32]


class GitWorktreeManager:
    """Creates isolated worktrees for a base workspace and integrates them back."""

    def __init__(self, base_workspace: str, *, root: Path | None = None) -> None:
        self._base = str(Path(base_workspace).expanduser().resolve()) if base_workspace else ""
        self._root = root or Path(tempfile.gettempdir()) / "north-worktrees"

    @property
    def base(self) -> str:
        return self._base

    async def is_git_repo(self) -> bool:
        """True when the base workspace is a git work tree with at least one commit.

        A worktree must branch from a commit, so a repo with an unborn HEAD (no
        commits yet) reports False and the caller falls back to non-isolated runs.
        """
        if not self._base:
            return False
        code, out, _ = await _run_git(["rev-parse", "--is-inside-work-tree"], self._base)
        if code != 0 or out.strip() != "true":
            return False
        code, _, _ = await _run_git(["rev-parse", "--verify", "HEAD"], self._base)
        return code == 0

    async def create(self, label: str) -> Worktree:
        """Add a linked worktree on a fresh ``north/wt-<label>-<rand>`` branch."""
        code, sha, err = await _run_git(["rev-parse", "HEAD"], self._base)
        if code != 0:
            raise WorktreeError(f"cannot resolve HEAD of {self._base}: {err.strip()}")
        base_sha = sha.strip()

        slug = _slug(label)
        uniq = uuid.uuid4().hex[:8]
        branch = f"north/wt-{slug}-{uniq}"
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        path = str((self._root / f"{slug}-{uniq}").resolve())

        code, _, err = await _run_git(["worktree", "add", "-b", branch, path, base_sha], self._base)
        if code != 0:
            raise WorktreeError(f"git worktree add failed: {err.strip()}")
        return Worktree(base=self._base, path=path, branch=branch, base_sha=base_sha)

    async def integrate(self, wt: Worktree, *, lock: asyncio.Lock | None = None) -> IntegrationResult:
        """Commit the worktree's changes and apply them back onto the base tree.

        Returns without touching the base tree when the agent produced no changes.
        When the resulting patch does not apply cleanly (another run changed the
        same lines), the base tree is left untouched and the branch is retained so
        the changes can be merged by hand. *lock*, when given, is held only for the
        apply so isolated runs serialise just for that step.
        """
        await _run_git(["add", "-A"], wt.path)
        # `--quiet` exits 1 when there are staged changes to commit, 0 when clean.
        code, _, _ = await _run_git(["diff", "--cached", "--quiet"], wt.path)
        if code == 1:
            c, _, err = await _run_git(
                [*_COMMIT_CONFIG, "commit", "--no-verify", "-m", "north: isolated agent changes"],
                wt.path,
            )
            if c != 0:
                raise WorktreeError(f"git commit in worktree failed: {err.strip()}")

        # Nothing actually diverged from base -> clean up, report no change.
        code, _, _ = await _run_git(["diff", "--quiet", wt.base_sha, "HEAD"], wt.path)
        if code == 0:
            await self.remove(wt, keep_branch=False)
            return IntegrationResult(applied=False, changed=False, conflicted=False, branch=wt.branch, path=wt.path)

        code, patch, err = await _run_git(["diff", "--binary", wt.base_sha, "HEAD"], wt.path, binary=True)
        if code != 0:
            raise WorktreeError(f"git diff failed: {err.strip()}")

        applied = await self._apply_back(wt, patch, lock=lock)
        await self.remove(wt, keep_branch=not applied)
        return IntegrationResult(
            applied=applied,
            changed=True,
            conflicted=not applied,
            branch=wt.branch,
            path=wt.path,
        )

    async def _apply_back(self, wt: Worktree, patch: bytes, *, lock: asyncio.Lock | None) -> bool:
        """Apply *patch* onto the base working tree; return False on any conflict."""
        if not patch.strip():
            return True
        if lock is not None:
            async with lock:
                return await self._apply_checked(wt, patch)
        return await self._apply_checked(wt, patch)

    async def _apply_checked(self, wt: Worktree, patch: bytes) -> bool:
        # Dry-run first so a conflict never leaves half-applied hunks or markers.
        code, _, err = await _run_git(["apply", "--check", "--whitespace=nowarn"], wt.base, input_data=patch)
        if code != 0:
            logger.info(
                "worktree integrate: patch from %s does not apply cleanly to %s (%s)",
                wt.branch,
                wt.base,
                err.strip()[:200],
            )
            return False
        code, _, err = await _run_git(["apply", "--whitespace=nowarn"], wt.base, input_data=patch)
        if code != 0:
            raise WorktreeError(f"git apply failed after --check passed: {err.strip()}")
        return True

    async def remove(self, wt: Worktree, *, keep_branch: bool) -> None:
        """Remove the linked worktree; delete its branch unless *keep_branch*."""
        code, _, err = await _run_git(["worktree", "remove", "--force", wt.path], wt.base)
        if code != 0:
            logger.warning("worktree remove failed for %s: %s", wt.path, err.strip())
            await _run_git(["worktree", "prune"], wt.base)
        if not keep_branch:
            await _run_git(["branch", "-D", wt.branch], wt.base)

    async def diff_line_count(self, wt: Worktree) -> int:
        """Total changed lines (insertions + deletions) in *wt* vs its base commit.

        Used as a best-of-N selection signal (a smaller, more surgical diff is
        preferred). Uncommitted work is included via a staged snapshot count.
        Returns 0 when nothing changed or the diff cannot be read.
        """
        await _run_git(["add", "-A"], wt.path)
        code, out, _ = await _run_git(["diff", "--cached", "--numstat", wt.base_sha], wt.path)
        if code != 0:
            return 0
        total = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                added = int(parts[0]) if parts[0].isdigit() else 0
                deleted = int(parts[1]) if parts[1].isdigit() else 0
                total += added + deleted
        return total
