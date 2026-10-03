"""Isolated git copies for coding edit runs, over the shared worktree manager."""

from __future__ import annotations

from coding_agents import CodingAgentError, FileDelta, WorkChange, WorkTree
from orchestrator.worktree import GitWorktreeManager, Worktree, WorktreeError


class GitWorkspaces:
    """A copy per run on a throwaway branch. Nothing is applied back to the real tree."""

    async def create(self, workspace: str, label: str) -> WorkTree:
        manager = GitWorktreeManager(workspace)
        if not await manager.is_git_repo():
            raise CodingAgentError(
                f"{workspace} is not a git repository with at least one commit, so there is nowhere isolated to work"
            )
        try:
            tree = await manager.create(label)
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc
        return WorkTree(tree.path, tree.branch, tree.base_sha, tree.base)

    async def finish(self, tree: WorkTree) -> WorkChange | None:
        """Commit what the agent changed; when it changed nothing, remove the copy and its branch."""
        manager = GitWorktreeManager(tree.base)
        copy = Worktree(base=tree.base, path=tree.path, branch=tree.branch, base_sha=tree.base_sha)
        try:
            if not await manager.commit_changes(copy):
                await manager.remove(copy, keep_branch=False)
                return None
            files = await manager.changes(copy)
        except WorktreeError as exc:
            raise CodingAgentError(str(exc)) from exc
        return WorkChange(tree, tuple(FileDelta(f.path, f.insertions, f.deletions) for f in files))
