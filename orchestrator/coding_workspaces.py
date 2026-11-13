"""Isolated git copies for coding edit runs, over the shared worktree manager."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from coding_agents import CodingAgentError, FileDelta, WorkChange, WorkTree
from orchestrator.worktree import GitWorktreeManager, Worktree, WorktreeError
from utils.clone import clone_tree

# Not the temp directory: both agents' sandboxes let a command write there, so one run could change another
# run's copy. Here a run's sandbox can write only to its own.
DEFAULT_ROOT = Path.home() / ".cache" / "north" / "coding-worktrees"
# Installed dependencies, which git ignores, so a copy git makes has none. Without them an agent could not run
# a single test and checked its work by reading it (benchmark, 2026-11-10), where the same agent run in the
# folder itself had them.
DEPENDENCY_DIRS = (".venv", "venv", "node_modules")
# The files of a virtualenv that name the folder it was made in: its scripts, and how it imports a project
# installed in editable mode. Left pointing at the real folder, the copy's tests would run the user's code.
_VENV_FILES = ("bin/*", "lib/python*/site-packages/*.pth", "lib/python*/site-packages/__editable__*")
# Compiled modules a build leaves next to the source (Python C and Rust extensions on each platform).
_EXTENSION_SUFFIXES = (".so", ".pyd", ".dylib")
_LIST_TIMEOUT_SECONDS = 30


class GitWorkspaces:
    """A copy per run on a throwaway branch. Nothing is applied back to the real tree."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or DEFAULT_ROOT

    async def create(self, workspace: str, label: str) -> WorkTree:
        manager = GitWorktreeManager(workspace, root=self._root)
        if not await manager.is_git_repo():
            raise CodingAgentError(
                f"{workspace} is not a git repository with at least one commit, so there is nowhere isolated to work"
            )
        try:
            tree = await manager.create(label)
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc
        made = WorkTree(tree.path, tree.branch, tree.base_sha, tree.base, tree.left_out)
        Path(made.start_in(workspace)).mkdir(parents=True, exist_ok=True)  # git does not track an empty folder
        await asyncio.to_thread(carry_dependencies, Path(made.base), Path(made.path))
        return made

    async def diff(self, tree: WorkTree) -> str:
        manager = GitWorktreeManager(tree.base)
        try:
            return await manager.diff_text(Worktree(tree.base, tree.path, tree.branch, tree.base_sha))
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc

    async def finish(self, tree: WorkTree, *, keep_empty: bool = False) -> WorkChange | None:
        """Commit what the agent changed; when the branch holds nothing, remove the copy and its branch.

        Decided from the branch, not this attempt: a resumed run that adds nothing still has what the
        earlier attempts committed. With *keep_empty* (a paused run) a copy with nothing on it stays too.
        """
        manager = GitWorktreeManager(tree.base)
        copy = Worktree(base=tree.base, path=tree.path, branch=tree.branch, base_sha=tree.base_sha)
        try:
            if reason := await manager.damage(copy):
                raise CodingAgentError(
                    f"the agent's isolated copy at {tree.path} was damaged ({reason}), so its changes could not be "
                    "saved. Its files are still there."
                )
            await manager.commit_changes(copy)
            if not await manager.has_changes(copy):
                if not keep_empty:
                    await manager.remove(copy, keep_branch=False)
                return None
            files = await manager.changes(copy)
            diff = await manager.diff_text(copy)
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc
        return WorkChange(tree, tuple(FileDelta(f.path, f.insertions, f.deletions) for f in files), diff)

    async def drop_if_in_place(self, tree: WorkTree) -> bool:
        """Delete *tree*'s branch when its changes are already in the working tree; True when it is gone."""
        manager = GitWorktreeManager(tree.base)
        try:
            return await manager.drop_branch_if_in_place(tree.branch, tree.base_sha)
        except WorktreeError:
            return False


def carry_dependencies(base: Path, copy: Path) -> None:
    """Clone the folder's installed dependencies into the copy, where a run's installs change only the clone.

    A clone, not a link: through a link an agent's `npm install` would change the user's own folder. Where
    this host cannot clone, the copy goes without, as before.
    """
    for name in DEPENDENCY_DIRS:
        source, target = base / name, copy / name
        if not source.is_dir() or source.is_symlink() or target.exists():
            continue
        if clone_tree(source, target) and (target / "pyvenv.cfg").is_file():
            _point_at_copy(target, base, copy)
    for relative in _built_extensions(base):
        source, target = base / relative, copy / relative
        if source.is_file() and not source.is_symlink() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            clone_tree(source, target)


def _built_extensions(base: Path) -> list[str]:
    """Native extensions built inside the folder, which git ignores and an editable install imports from there.

    pydantic keeps pydantic-core in its repository; its compiled module was not in the copy, so nothing
    could import pydantic and not one test ran (benchmark, 2026-11-12).
    """
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(base),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            *(f"*{suffix}" for suffix in _EXTENSION_SUFFIXES),
            *(f":(exclude){name}" for name in DEPENDENCY_DIRS),
        ],
        capture_output=True,
        text=True,
        timeout=_LIST_TIMEOUT_SECONDS,
    )
    return [path for path in listed.stdout.split("\0") if path] if listed.returncode == 0 else []


def _point_at_copy(venv: Path, base: Path, copy: Path) -> None:
    """Make a cloned virtualenv run the copy's code: where its files name the real folder, name the copy."""
    real = re.compile(re.escape(str(base)) + r"(?=[/\"'\s]|$)", re.MULTILINE)
    for file in (found for pattern in _VENV_FILES for found in venv.glob(pattern)):
        if file.is_symlink() or not file.is_file():
            continue
        try:
            text = file.read_text()
        except (OSError, UnicodeDecodeError):  # a binary in bin/
            continue
        if real.search(text):
            file.write_text(real.sub(str(copy), text))  # the clone's own blocks: the original is untouched
