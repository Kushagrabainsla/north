"""Isolated git copies for coding edit runs, over the shared worktree manager."""

from __future__ import annotations

from pathlib import Path

from coding_agents import CodingAgentError, FileDelta, WorkChange, WorkTree
from orchestrator.worktree import GitWorktreeManager, Worktree, WorktreeError

# Not the temp directory: both agents' sandboxes let a command write there, so one run could change another
# run's copy. Here a run's sandbox can write only to its own.
DEFAULT_ROOT = Path.home() / ".cache" / "north" / "coding-worktrees"


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
        return WorkTree(tree.path, tree.branch, tree.base_sha, tree.base)

    async def diff(self, tree: WorkTree) -> str:
        manager = GitWorktreeManager(tree.base)
        try:
            return await manager.diff_text(Worktree(tree.base, tree.path, tree.branch, tree.base_sha))
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc

    async def finish(self, tree: WorkTree) -> WorkChange | None:
        """Commit what the agent changed; when it changed nothing, remove the copy and its branch."""
        manager = GitWorktreeManager(tree.base)
        copy = Worktree(base=tree.base, path=tree.path, branch=tree.branch, base_sha=tree.base_sha)
        try:
            if reason := await manager.damage(copy):
                raise CodingAgentError(
                    f"the agent's isolated copy at {tree.path} was damaged ({reason}), so its changes could not be "
                    "saved. Its files are still there."
                )
            if not await manager.commit_changes(copy):
                await manager.remove(copy, keep_branch=False)
                return None
            files = await manager.changes(copy)
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc
        return WorkChange(tree, tuple(FileDelta(f.path, f.insertions, f.deletions) for f in files))
