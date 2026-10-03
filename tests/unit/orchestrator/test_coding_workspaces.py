"""Isolated copies for edit runs, against real git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agents import CodingAgentError
from orchestrator.coding_workspaces import GitWorkspaces


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    _git(["init", "-q"], directory)
    _git(["config", "user.name", "t"], directory)
    _git(["config", "user.email", "t@t"], directory)
    (directory / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(["add", "-A"], directory)
    _git(["commit", "-qm", "init"], directory)
    return directory


async def test_a_copy_is_made_on_a_throwaway_branch_and_remembers_where_it_came_from(repo, tmp_path) -> None:
    tree = await GitWorkspaces(tmp_path / "copies").create(str(repo), "coding-abc12345")

    assert Path(tree.path, "calc.py").is_file() and tree.branch.startswith("north/wt-coding-abc12345")
    assert tree.base == str(repo.resolve()) and tree.base_sha == _git(["rev-parse", "HEAD"], repo).strip()


async def test_a_folder_that_is_not_a_repository_has_nowhere_isolated_to_work(tmp_path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()

    with pytest.raises(CodingAgentError, match="not a git repository"):
        await GitWorkspaces(tmp_path / "copies").create(str(folder), "x")


async def test_finishing_commits_the_change_to_the_branch_and_leaves_the_real_tree_alone(repo, tmp_path) -> None:
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-edit")
    Path(tree.path, "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")

    change = await workspaces.finish(tree)

    assert [(f.path, f.insertions, f.deletions) for f in change.files] == [("calc.py", 3, 0)]
    assert (change.insertions, change.deletions) == (3, 0)
    assert "sub" in _git(["show", f"{tree.branch}:calc.py"], repo)
    assert "sub" not in (repo / "calc.py").read_text(), "nothing is applied to the real tree"
    assert Path(tree.path).is_dir(), "the copy stays for review"


async def test_finishing_with_no_change_removes_the_copy_and_its_branch(repo, tmp_path) -> None:
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-quiet")

    assert await workspaces.finish(tree) is None

    assert not Path(tree.path).exists()
    assert tree.branch not in _git(["branch", "--list"], repo)


async def test_copies_are_made_outside_the_temp_directory_by_default() -> None:
    import tempfile

    from orchestrator.coding_workspaces import DEFAULT_ROOT

    assert not str(DEFAULT_ROOT).startswith(tempfile.gettempdir()), "an agent's sandbox may write to temp"
    assert DEFAULT_ROOT.is_relative_to(Path.home())


async def test_the_diff_of_a_finished_change_is_readable_without_applying_it(repo, tmp_path) -> None:
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-diff")
    Path(tree.path, "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
    await workspaces.finish(tree)

    text = await workspaces.diff(tree)

    assert "+def sub(a, b):" in text and "return a - b" in text
    assert "sub" not in (repo / "calc.py").read_text()
