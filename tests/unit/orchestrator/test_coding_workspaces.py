"""Isolated copies for edit runs, against real git."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coding_agents import CodingAgentError
from coding_agents.runner import _in_chosen_folder
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


macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="clones folders with APFS clonefile")


def _with_dependencies(repo: Path) -> None:
    """A node_modules and an editable-install virtualenv, both ignored, as a set-up project has."""
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "ignore"], repo)
    (repo / "node_modules" / "dep").mkdir(parents=True)
    (repo / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
    venv = repo / ".venv"
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "pytest").write_text(f"#!{repo.resolve()}/.venv/bin/python\nimport pytest\n")
    (site / "_editable_calc.pth").write_text(f"{repo.resolve()}\n")
    (site / "other.pth").write_text(f"{repo.resolve()}-elsewhere\n")


@macos_only
async def test_a_copy_has_the_installed_dependencies_pointing_at_itself(repo, tmp_path) -> None:
    _with_dependencies(repo)

    tree = await GitWorkspaces(tmp_path / "copies").create(str(repo), "coding-deps")

    copy = Path(tree.path)
    assert (copy / "node_modules" / "dep" / "index.js").read_text() == "module.exports = 1\n"
    site = copy / ".venv" / "lib" / "python3.12" / "site-packages"
    assert (site / "_editable_calc.pth").read_text() == f"{copy}\n", "the copy imports its own code"
    assert (copy / ".venv" / "bin" / "pytest").read_text().startswith(f"#!{copy}/.venv/bin/python")
    assert (site / "other.pth").read_text() == f"{repo.resolve()}-elsewhere\n", "another folder is not renamed"
    assert (repo / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_calc.pth").read_text() == (
        f"{repo.resolve()}\n"
    ), "and the user's own virtualenv is untouched"


@macos_only
async def test_a_native_extension_built_in_the_folder_is_in_the_copy(repo, tmp_path) -> None:
    """pydantic's in-repo pydantic-core: its compiled module is ignored, and nothing imports without it."""
    _with_dependencies(repo)
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n*.so\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "ignore built"], repo)
    package = repo / "core" / "python" / "core"
    package.mkdir(parents=True)
    (package / "_core.cpython-311-darwin.so").write_bytes(b"\x7fELF built")
    (repo / ".venv" / "lib" / "python3.12" / "site-packages" / "skip.so").write_bytes(b"in the venv")

    tree = await GitWorkspaces(tmp_path / "copies").create(str(repo), "coding-ext")

    copied = Path(tree.path, "core", "python", "core", "_core.cpython-311-darwin.so")
    assert copied.read_bytes() == b"\x7fELF built"
    assert await GitWorkspaces(tmp_path / "copies").finish(tree) is None, "never part of the change"


@macos_only
async def test_an_install_in_the_copy_does_not_reach_the_users_folder(repo, tmp_path) -> None:
    _with_dependencies(repo)
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-deps")

    Path(tree.path, "node_modules", "dep", "index.js").write_text("changed\n")
    (Path(tree.path) / "node_modules" / "new").mkdir()

    assert (repo / "node_modules" / "dep" / "index.js").read_text() == "module.exports = 1\n"
    assert not (repo / "node_modules" / "new").exists()
    assert await workspaces.finish(tree) is None, "dependencies are never part of the change"
    assert not Path(tree.path).exists(), "and they go with the copy"


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


async def test_a_resumed_run_that_adds_nothing_keeps_what_earlier_attempts_committed(repo, tmp_path) -> None:
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-resumed")
    Path(tree.path, "calc.py").write_text("# partial work\n")
    await workspaces.finish(tree)

    change = await workspaces.finish(tree)  # the resumed attempt changed nothing new

    assert change is not None and [f.path for f in change.files] == ["calc.py"]
    assert Path(tree.path).is_dir()
    assert tree.branch in _git(["branch", "--list"], repo)


async def test_a_paused_run_with_nothing_on_it_keeps_its_copy_to_resume_in(repo, tmp_path) -> None:
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "coding-paused")

    assert await workspaces.finish(tree, keep_empty=True) is None

    assert Path(tree.path).is_dir()


async def test_a_copy_of_a_subfolder_starts_there_and_knows_the_repository(repo, tmp_path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("two\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "sub"], repo)
    (sub / "untracked.txt").write_text("mine\n")

    tree = await GitWorkspaces(tmp_path / "copies").create(str(sub), "coding-sub")

    assert tree.base == str(repo.resolve())
    assert tree.start_in(str(sub)) == str(Path(tree.path) / "sub")
    assert Path(tree.path, "sub", "untracked.txt").is_file()
    assert not Path(tree.path, "untracked.txt").exists()


async def test_the_agent_is_told_which_subfolder_was_chosen(repo, tmp_path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    tree = await GitWorkspaces(tmp_path / "copies").create(str(sub), "coding-told")

    assert "`sub`" in _in_chosen_folder("Fix it.", tree, str(sub))
    assert _in_chosen_folder("Fix it.", tree, str(repo)) == "Fix it."


async def test_a_chosen_folder_git_does_not_track_exists_in_the_copy(repo, tmp_path) -> None:
    empty = repo / "empty"
    empty.mkdir()

    tree = await GitWorkspaces(tmp_path / "copies").create(str(empty), "coding-empty")

    assert Path(tree.start_in(str(empty))).is_dir()


async def test_a_repository_nested_in_the_folder_is_left_out_and_named(repo, tmp_path) -> None:
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git(["init", "-q"], nested)

    tree = await GitWorkspaces(tmp_path / "copies").create(str(repo), "coding-nested")

    assert tree.left_out == ("vendor/lib",)
    assert not Path(tree.path, "vendor", "lib").exists()


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


async def test_a_copy_whose_git_link_was_deleted_is_reported_not_called_unchanged(repo, tmp_path) -> None:
    """The silent case: the agent edits, then deletes `.git`; north used to say "no changes" and leak the copy."""
    workspaces = GitWorkspaces(tmp_path / "copies")
    tree = await workspaces.create(str(repo), "damaged")
    Path(tree.path, "calc.py").write_text("x = 2  # the agent's edit\n")
    (Path(tree.path) / ".git").unlink()

    with pytest.raises(CodingAgentError) as excinfo:
        await workspaces.finish(tree)

    assert tree.path in str(excinfo.value) and "damaged" in str(excinfo.value)
    assert Path(tree.path, "calc.py").read_text() == "x = 2  # the agent's edit\n", (
        "the work is left where it can be found"
    )
