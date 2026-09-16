"""Stable identity for repository evidence cited by engineering agents."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositoryIdentity:
    commit: str
    dirty: bool

    def render(self) -> str:
        state = "has uncommitted changes" if self.dirty else "clean"
        return (
            "## Repository evidence identity\n"
            f"- Git commit: `{self.commit}`\n"
            f"- Worktree: {state}\n"
            "- Cite repository claims as "
            f"`{self.commit}:path/to/file:symbol-or-line`. "
            "If the cited evidence is uncommitted, label it `working-tree` as well."
        )


def repository_identity(workspace: str) -> RepositoryIdentity | None:
    """Return the current commit and dirty state, or None outside a Git worktree."""
    root = Path(workspace)
    if not root.is_dir():
        return None
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
        commit = revision.stdout.strip()
        invalid_hash = len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower())
        if revision.returncode != 0 or invalid_hash:
            return None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
        return RepositoryIdentity(commit=commit, dirty=status.returncode != 0 or bool(status.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        return None
