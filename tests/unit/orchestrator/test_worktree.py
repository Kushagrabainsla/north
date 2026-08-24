"""Unit tests for git worktree isolation (orchestrator/worktree.py).

Exercises real git against throwaway repositories in tmp_path: creating an
isolated worktree, applying its changes back, the no-change and conflict paths,
and file deletion. No mocking - the value is in the actual git behaviour.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import GitWorktreeManager


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo(path: Path, *, readme: str = "line1\nline2\nline3\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.name", "test"], path)
    _git(["config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text(readme)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)


def _branches(path: Path) -> str:
    return _git(["branch", "--list"], path).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    _init_repo(base)
    return base


@pytest.fixture
def manager(repo: Path, tmp_path: Path) -> GitWorktreeManager:
    return GitWorktreeManager(str(repo), root=tmp_path / "worktrees")


async def test_is_git_repo_true_for_repo_with_commit(manager: GitWorktreeManager) -> None:
    assert await manager.is_git_repo() is True


async def test_is_git_repo_false_for_non_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await GitWorktreeManager(str(plain), root=tmp_path / "wt").is_git_repo() is False


async def test_is_git_repo_false_for_unborn_repo(tmp_path: Path) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(["init", "-q"], unborn)
    assert await GitWorktreeManager(str(unborn), root=tmp_path / "wt").is_git_repo() is False


async def test_create_isolates_from_base(manager: GitWorktreeManager, repo: Path) -> None:
    wt = await manager.create("coder")
    wt_path = Path(wt.path)

    assert wt_path.is_dir()
    assert (wt_path / "README.md").exists()
    assert wt.branch in _branches(repo)

    # A new file in the worktree must not leak into the base tree.
    (wt_path / "scratch.txt").write_text("only in worktree\n")
    assert not (repo / "scratch.txt").exists()

    await manager.remove(wt, keep_branch=False)


async def test_integrate_applies_new_and_modified(manager: GitWorktreeManager, repo: Path) -> None:
    wt = await manager.create("coder")
    wt_path = Path(wt.path)
    (wt_path / "feature.py").write_text("print('hi')\n")
    (wt_path / "README.md").write_text("line1\nCHANGED\nline3\n")

    result = await manager.integrate(wt)

    assert result.applied is True
    assert result.changed is True
    assert result.conflicted is False
    # Changes landed in the base working tree.
    assert (repo / "feature.py").read_text() == "print('hi')\n"
    assert (repo / "README.md").read_text() == "line1\nCHANGED\nline3\n"
    # Worktree and branch cleaned up.
    assert not wt_path.exists()
    assert wt.branch not in _branches(repo)


async def test_diff_line_count_counts_added_and_deleted(manager: GitWorktreeManager) -> None:
    wt = await manager.create("coder")
    wt_path = Path(wt.path)
    (wt_path / "new.py").write_text("a\nb\nc\n")  # 3 insertions
    (wt_path / "README.md").write_text("line1\nX\nline3\n")  # 1 del + 1 add on line 2
    count = await manager.diff_line_count(wt)
    assert count == 5  # 3 (new file) + 2 (one line changed = -1 +1)
    await manager.remove(wt, keep_branch=False)


async def test_diff_line_count_zero_when_unchanged(manager: GitWorktreeManager) -> None:
    wt = await manager.create("coder")
    assert await manager.diff_line_count(wt) == 0
    await manager.remove(wt, keep_branch=False)


async def test_integrate_noop_when_no_changes(manager: GitWorktreeManager, repo: Path) -> None:
    wt = await manager.create("coder")

    result = await manager.integrate(wt)

    assert result.changed is False
    assert result.applied is False
    assert not Path(wt.path).exists()
    assert wt.branch not in _branches(repo)


async def test_integrate_applies_file_deletion(manager: GitWorktreeManager, repo: Path) -> None:
    wt = await manager.create("coder")
    (Path(wt.path) / "README.md").unlink()

    result = await manager.integrate(wt)

    assert result.applied is True
    assert not (repo / "README.md").exists()


async def test_integrate_conflict_keeps_branch(manager: GitWorktreeManager, repo: Path) -> None:
    wt = await manager.create("coder")
    # Worktree edits line2; base working tree edits the SAME line differently.
    (Path(wt.path) / "README.md").write_text("line1\nWORKTREE\nline3\n")
    (repo / "README.md").write_text("line1\nBASE_LOCAL\nline3\n")

    result = await manager.integrate(wt)

    assert result.applied is False
    assert result.conflicted is True
    assert result.changed is True
    # Base's own edit is preserved, not clobbered.
    assert (repo / "README.md").read_text() == "line1\nBASE_LOCAL\nline3\n"
    # Worktree removed, but the branch is retained for manual merge.
    assert not Path(wt.path).exists()
    assert wt.branch in _branches(repo)
    # Clean up the retained branch so tmp_path teardown is tidy.
    _git(["branch", "-D", wt.branch], repo)


async def test_disjoint_integrations_serialize_under_lock(manager: GitWorktreeManager, repo: Path) -> None:
    """Two worktrees touching different files both apply when sharing one lock."""
    lock = asyncio.Lock()
    wt_a = await manager.create("coder-a")
    wt_b = await manager.create("coder-b")
    (Path(wt_a.path) / "a.txt").write_text("A\n")
    (Path(wt_b.path) / "b.txt").write_text("B\n")

    res_a, res_b = await asyncio.gather(
        manager.integrate(wt_a, lock=lock),
        manager.integrate(wt_b, lock=lock),
    )

    assert res_a.applied and res_b.applied
    assert (repo / "a.txt").read_text() == "A\n"
    assert (repo / "b.txt").read_text() == "B\n"


@pytest.mark.asyncio
async def test_worktree_integrates_binary_file_cleanly(repo: Path, tmp_path: Path) -> None:
    """Verify non-UTF8 binary files integrate without byte corruption."""
    mgr = GitWorktreeManager(str(repo), root=tmp_path / "wts")
    wt = await mgr.create("binary-agent")

    binary_data = b"\x00\xff\xfe\x80\x01\x02\x03PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    bin_file = Path(wt.path) / "image.png"
    bin_file.write_bytes(binary_data)

    res = await mgr.integrate(wt)
    assert res.applied is True
    assert res.changed is True
    assert (repo / "image.png").read_bytes() == binary_data

