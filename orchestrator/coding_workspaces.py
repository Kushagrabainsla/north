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
        made = WorkTree(tree.path, tree.branch, tree.base_sha, tree.base, tree.left_out)
        Path(made.start_in(workspace)).mkdir(parents=True, exist_ok=True)  # git does not track an empty folder
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
