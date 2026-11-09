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
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from utils.filesystem import CACHE_DIRS
from utils.processes import stop_process_group

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
    left_out: tuple[str, ...] = ()  # repositories nested in the folder, which the copy does not have


@dataclass(frozen=True)
class FileChange:
    """One file's change in a worktree, against the commit it branched from."""

    path: str
    insertions: int
    deletions: int


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
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=input_data), timeout=timeout)
    except TimeoutError as exc:
        raise WorktreeError(f"git {args[0] if args else ''} timed out after {timeout}s") from exc
    finally:
        await stop_process_group(proc)
    stdout = out if binary else out.decode(errors="replace")
    return proc.returncode, stdout, err.decode(errors="replace")


def _slug(label: str) -> str:
    """Filesystem/branch-safe short slug derived from *label*."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in label).strip("-")
    return (safe or "agent")[:32]


_MAX_UNTRACKED_BYTES = 5_000_000

# Before a branch with work on it goes, a hidden ref keeps that work reachable for a while: `git log` and
# `git branch` do not show it, and it survives `branch -D` and garbage collection until it expires.
_KEPT_REFS = "refs/north/kept"
_KEPT_SECONDS = 14 * 24 * 3600

# Pathspecs leaving out the caches an agent's runs write, which are never its change. A folder north had
# set up with `git init` has no .gitignore, and the `__pycache__` of a test run was applied to it (#76, M24).
_LEAVING_CACHES = tuple(f":(exclude,glob)**/{name}/**" for name in sorted(CACHE_DIRS))


async def _stage_agent_work(path: str) -> None:
    """Stage everything the agent changed in the copy at *path*, but not the caches its runs left there."""
    await _run_git(["add", "-A", "--", ".", *_LEAVING_CACHES], path)


def _copy_untracked(base: Path, copy: Path, name: str) -> bool:
    """One untracked file of the user's into the copy, unless it is inside a `.git` or too big to carry.

    False for a whole git repository nested in the folder, which git lists as one directory: it is
    not north's to copy, and copying it as a file failed every edit run in that folder.
    """
    source = base / name
    if source.is_dir() and not source.is_symlink():
        return False
    if ".git" in Path(name).parts or (not source.is_symlink() and source.stat().st_size > _MAX_UNTRACKED_BYTES):
        return True
    target = copy / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    return True


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
        """Add a linked worktree on a fresh ``north/wt-<label>-<rand>`` branch.

        The worktree's ``base`` is the repository's top level, whichever folder of it was chosen:
        git commands run in a subfolder see only part of it, and `git apply` silently skips the rest.
        """
        code, top, err = await _run_git(["rev-parse", "--show-toplevel"], self._base)
        if code != 0:
            raise WorktreeError(f"cannot find the repository of {self._base}: {err.strip()}")
        base = top.strip()
        code, sha, err = await _run_git(["rev-parse", "HEAD"], base)
        if code != 0:
            raise WorktreeError(f"cannot resolve HEAD of {base}: {err.strip()}")
        base_sha = sha.strip()

        slug = _slug(label)
        uniq = uuid.uuid4().hex[:8]
        branch = f"north/wt-{slug}-{uniq}"
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        path = str((self._root / f"{slug}-{uniq}").resolve())

        code, _, err = await _run_git(["worktree", "add", "-b", branch, path, base_sha], base)
        if code != 0:
            raise WorktreeError(f"git worktree add failed: {err.strip()}")
        created = Worktree(base=base, path=path, branch=branch, base_sha=base_sha)
        try:
            seeded_sha, left_out = await self._seed(created)
        except BaseException as exc:
            await self.remove(created, keep_branch=False)
            if isinstance(exc, OSError):
                raise WorktreeError(f"your files could not be carried into the copy: {exc}") from exc
            raise
        return replace(created, base_sha=seeded_sha, left_out=left_out)

    async def _seed(self, wt: Worktree) -> tuple[str, tuple[str, ...]]:
        """Start the copy from what the user's working tree holds now; return its start and what was left out.

        Not only from the last commit.

        A change an earlier run applied is uncommitted there. Without it a later run on the same task would
        be working on code that no longer matches, and could not see the fix it was asked to build on. What is
        carried over is committed as the copy's first commit, and that commit is the base the agent's own work
        is measured and applied against, so only the agent's edits ever go back.
        """
        path = wt.path
        code, patch, err = await _run_git(["diff", "--binary", "HEAD"], wt.base, binary=True)
        if code != 0:
            raise WorktreeError(f"git diff failed: {err.strip()}")
        code, listed, err = await _run_git(["ls-files", "--others", "--exclude-standard", "-z"], wt.base)
        if code != 0:
            raise WorktreeError(f"git ls-files failed: {err.strip()}")
        untracked = [name for name in listed.split("\0") if name]
        if not patch.strip() and not untracked:
            return wt.base_sha, ()
        if patch.strip():
            code, _, err = await _run_git(["apply", "--binary", "--whitespace=nowarn"], path, input_data=patch)
            if code != 0:
                raise WorktreeError(f"your uncommitted changes could not be carried into the copy: {err.strip()}")
        left_out = []
        for name in untracked:
            if not await asyncio.to_thread(_copy_untracked, Path(wt.base), Path(path), name):
                left_out.append(name.rstrip("/"))
        await _run_git(["add", "-A"], path)
        code, _, err = await _run_git(
            [*_COMMIT_CONFIG, "commit", "--no-verify", "--allow-empty", "-m", "north: your uncommitted changes"], path
        )
        if code != 0:
            raise WorktreeError(f"git commit in worktree failed: {err.strip()}")
        code, sha, err = await _run_git(["rev-parse", "HEAD"], path)
        if code != 0:
            raise WorktreeError(f"cannot resolve the copy's start: {err.strip()}")
        return sha.strip(), tuple(left_out)

    async def integrate(self, wt: Worktree, *, lock: asyncio.Lock | None = None) -> IntegrationResult:
        """Commit the worktree's changes and apply them back onto the base tree.

        Returns without touching the base tree when the agent produced no changes.
        When the resulting patch does not apply cleanly (another run changed the
        same lines), the base tree is left untouched and the branch is retained so
        the changes can be merged by hand. *lock*, when given, is held only for the
        apply so isolated runs serialise just for that step.
        """
        await self.commit_changes(wt)

        # Nothing actually diverged from base -> clean up, report no change.
        if not await self.has_changes(wt):
            await self.remove(wt, keep_branch=False)
            return IntegrationResult(applied=False, changed=False, conflicted=False, branch=wt.branch, path=wt.path)

        applied = await self.apply_back(wt, lock=lock)
        await self.remove(wt, keep_branch=not applied)
        return IntegrationResult(
            applied=applied,
            changed=True,
            conflicted=not applied,
            branch=wt.branch,
            path=wt.path,
        )

    async def apply_back(self, wt: Worktree, *, lock: asyncio.Lock | None = None) -> bool:
        """Apply what *wt*'s branch changed onto the base working tree, as uncommitted changes.

        Returns False, with the base tree untouched, when the patch does not apply cleanly (the same lines
        changed there). *lock*, when given, is held only for the apply. Removing the copy is the caller's.
        """
        code, patch, err = await _run_git(["diff", "--binary", wt.base_sha, "HEAD"], wt.path, binary=True)
        if code != 0:
            raise WorktreeError(f"git diff failed: {err.strip()}")
        return await self._apply_back(wt, patch, lock=lock)

    async def diff_text(self, wt: Worktree) -> str:
        """What *wt*'s branch tip changed against the commit it branched from, as a unified diff."""
        code, out, err = await _run_git(["diff", "--no-color", wt.base_sha, "HEAD"], wt.path)
        if code != 0:
            raise WorktreeError(f"git diff failed: {err.strip()}")
        return str(out)

    async def damage(self, wt: Worktree) -> str:
        """Why *wt* can no longer be trusted to commit onto its own branch, or "" when it is intact.

        An agent can delete or repoint a copy's `.git` link. Without the link nothing can be committed, and
        pointing it at the real repository would aim a commit there.
        """
        if not (Path(wt.path) / ".git").is_file():
            return "its .git link is gone"
        code, here, _ = await _run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], wt.path)
        base_code, there, _ = await _run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self._base)
        if code != 0 or base_code != 0 or os.path.realpath(here.strip()) != os.path.realpath(there.strip()):
            return "its .git link no longer leads to north's repository"
        code, branch, _ = await _run_git(["symbolic-ref", "--short", "HEAD"], wt.path)
        if code != 0 or branch.strip() != wt.branch:
            return f"it is no longer on its branch {wt.branch}"
        return ""

    async def commit_changes(self, wt: Worktree) -> bool:
        """Commit everything changed in *wt* onto its branch; False when nothing changed.

        Hooks are skipped: the worktree belongs to an agent, and a hook it planted must not run.
        """
        await _stage_agent_work(wt.path)
        # `--quiet` exits 1 when there are staged changes to commit, 0 when clean.
        code, _, _ = await _run_git(["diff", "--cached", "--quiet"], wt.path)
        if code != 1:
            return False
        code, _, err = await _run_git(
            [*_COMMIT_CONFIG, "commit", "--no-verify", "-m", "north: isolated agent changes"], wt.path
        )
        if code != 0:
            raise WorktreeError(f"git commit in worktree failed: {err.strip()}")
        return True

    async def has_changes(self, wt: Worktree) -> bool:
        """Whether *wt*'s branch differs from the commit it branched from, however many attempts made it so."""
        code, _, _ = await _run_git(["diff", "--quiet", wt.base_sha, "HEAD"], wt.path)
        return code != 0

    async def changes(self, wt: Worktree) -> tuple[FileChange, ...]:
        """The files that differ between *wt*'s branch tip and the commit it branched from."""
        code, out, _ = await _run_git(["diff", "--numstat", wt.base_sha, "HEAD"], wt.path)
        if code != 0:
            return ()
        files = []
        for line in out.splitlines():
            added, deleted, path = line.split("\t", 2)  # a binary file shows "-" for both counts
            files.append(
                FileChange(path, int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
            )
        return tuple(files)

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
        # `--verbose` because a patch outside the current folder is skipped silently, and git still exits 0.
        code, _, err = await _run_git(
            ["apply", "--check", "--verbose", "--whitespace=nowarn"], wt.base, input_data=patch
        )
        if code != 0 or "Skipped patch" in err:
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
            await self._keep_way_back(wt)
            await _run_git(["branch", "-D", wt.branch], wt.base)

    async def _keep_way_back(self, wt: Worktree) -> None:
        """Leave a hidden ref at *wt*'s work before its branch goes, and expire old ones."""
        code, _, _ = await _run_git(["diff", "--quiet", wt.base_sha, wt.branch], wt.base)
        if code != 1:
            return
        await _run_git(["update-ref", f"{_KEPT_REFS}/{wt.branch.removeprefix('north/')}", wt.branch], wt.base)
        listing = ["for-each-ref", "--format=%(refname) %(committerdate:unix)", _KEPT_REFS]
        code, out, _ = await _run_git(listing, wt.base)
        if code != 0:
            return
        oldest_kept = time.time() - _KEPT_SECONDS
        for line in out.splitlines():
            ref, committed = line.rsplit(" ", 1)
            if int(committed) < oldest_kept:
                await _run_git(["update-ref", "-d", ref], wt.base)

    async def drop_branch_if_in_place(self, branch: str, base_sha: str) -> bool:
        """Delete *branch* when everything it changed is already in the working tree; True when it is gone.

        Safe by construction: a branch is deleted only when, for every file it changed, the working tree
        holds the same content, so nothing on it is lost. A branch that no longer exists counts as gone.
        """
        code, _, _ = await _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], self._base)
        if code != 0:
            return True
        code, out, _ = await _run_git(["diff", "--name-only", "-z", base_sha, branch], self._base)
        if code != 0:
            return False
        for path in (path for path in out.split("\0") if path):
            if not await self._same_as_branch(branch, path):
                return False
        code, _, err = await _run_git(["branch", "-D", branch], self._base)
        if code != 0:
            logger.warning("could not delete branch %s: %s", branch, err.strip())
            return False
        return True

    async def _same_as_branch(self, branch: str, path: str) -> bool:
        """Whether the working tree's *path* holds what *branch* has there, absence included.

        Compared by content hash, not `git diff`: an applied change leaves new files untracked, and git
        diff does not look at untracked files.
        """
        code, wanted, _ = await _run_git(["rev-parse", "--verify", "--quiet", f"{branch}:{path}"], self._base)
        on_disk = Path(self._base) / path
        if code != 0:  # the branch deleted it
            return not on_disk.exists()
        if not on_disk.is_file():
            return False
        code, actual, _ = await _run_git(["hash-object", "--", path], self._base)
        return code == 0 and actual.strip() == wanted.strip()
