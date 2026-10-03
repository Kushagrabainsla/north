"""Small helpers the live tests share."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from orchestrator.coding_workspaces import GitWorkspaces


def status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout


def head(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True).stdout


def workspaces() -> GitWorkspaces:
    """Copies in a throwaway directory, so a live test never leaves anything in the real cache."""
    return GitWorkspaces(Path(tempfile.mkdtemp(prefix="north-live-copies-")))
