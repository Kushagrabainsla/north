"""Repository evidence receives a stable Git identity."""

from __future__ import annotations

import subprocess
from pathlib import Path

from context.repo_revision import repository_identity


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def test_repository_identity_reports_commit_and_dirty_worktree(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "North Test")
    _git(tmp_path, "config", "user.email", "north@example.test")
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "initial")

    clean = repository_identity(str(tmp_path))

    assert clean is not None
    assert clean.commit == _git(tmp_path, "rev-parse", "HEAD")
    assert clean.dirty is False
    assert f"{clean.commit}:path/to/file:symbol-or-line" in clean.render()

    source.write_text("def run():\n    return 2\n", encoding="utf-8")
    dirty = repository_identity(str(tmp_path))
    assert dirty is not None and dirty.dirty is True


def test_repository_identity_is_absent_outside_git(tmp_path: Path) -> None:
    assert repository_identity(str(tmp_path)) is None
