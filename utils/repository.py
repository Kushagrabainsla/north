"""Stable identity for a local Git repository."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositoryIdentity:
    commit: str
    dirty: bool
    project_id: str = ""
    workspace_id: str = ""

    def render(self) -> str:
        state = "has uncommitted changes" if self.dirty else "clean"
        return (
            "## Repository evidence identity\n"
            f"- Project: `{self.project_id or 'unknown'}`\n"
            f"- Workspace: `{self.workspace_id or 'unknown'}`\n"
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
            capture_output=True, check=False, text=True, timeout=3,
        )
        commit = revision.stdout.strip()
        invalid_hash = len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower())
        if revision.returncode != 0 or invalid_hash:
            return None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
            capture_output=True, check=False, text=True, timeout=3,
        )
        root_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, check=False, text=True, timeout=3,
        )
        canonical_root = str(Path(root_result.stdout.strip() or root).resolve())
        remote_result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, check=False, text=True, timeout=3,
        )
        remote = remote_result.stdout.strip()
        return RepositoryIdentity(
            commit=commit,
            dirty=status.returncode != 0 or bool(status.stdout.strip()),
            project_id=remote or f"local:{canonical_root}",
            workspace_id="workspace:" + hashlib.sha256(canonical_root.encode()).hexdigest()[:16],
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
