"""What a Codex run may touch: the home folder is closed, and opened only for what the run needs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agents import Mode
from coding_agents.confinement import COPIES_DIR, codex_filesystem, profile


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    for name in (
        ".config",
        ".kube",
        ".ssh",
        "Documents",
        ".local/bin",
        ".local/share/keyrings",
        ".cache/north",
        ".codex",
    ):
        (folder / name).mkdir(parents=True)
    (folder / ".netrc").write_text("machine example login me password secret\n")
    return folder


def _rules(home: Path, workspace: Path, mode: Mode = Mode.EDIT, **extra) -> dict[str, str]:
    return codex_filesystem(str(workspace), mode, extra.pop("protected", ()), extra.pop("command_dirs", ()), home=home)


def test_a_credential_nobody_listed_is_closed(home, tmp_path) -> None:
    rules = _rules(home, tmp_path / "repo")

    for name in (".config", ".kube", ".netrc", "Documents"):
        assert rules[str(home / name)] == "deny", name


def test_the_home_folder_itself_is_not_denied_so_git_can_still_look_at_it(home, tmp_path) -> None:
    assert str(home) not in _rules(home, tmp_path / "repo")


def test_what_a_run_needs_stays_open(home, tmp_path) -> None:
    rules = _rules(home, tmp_path / "repo", command_dirs=["/opt/tools/bin"])

    assert rules[str(home / ".codex")] == "write"
    assert rules[str(home / ".local" / "bin")] == "read"
    assert rules[str(home / ".cache")] == "write"
    assert rules["/opt/tools/bin"] == "read"


def test_a_folder_on_the_way_to_something_open_is_walked_not_closed(home, tmp_path) -> None:
    """`.local` holds an open toolchain and a keyring: the toolchain stays open, the keyring does not."""
    rules = _rules(home, tmp_path / "repo")

    assert str(home / ".local") not in rules
    assert rules[str(home / ".local" / "share")] == "deny"
    assert rules[str(home / ".local" / "bin")] == "read"


def test_the_workspace_inside_the_home_folder_is_open_and_its_neighbours_are_not(home) -> None:
    workspace = home / "Documents" / "projects" / "mine"
    workspace.mkdir(parents=True)
    (home / "Documents" / "projects" / "other").mkdir()
    (home / "Documents" / "taxes").mkdir()

    rules = _rules(home, workspace)

    assert rules[str(workspace.resolve())] == "write"
    assert rules[str(home / "Documents" / "projects" / "other")] == "deny"
    assert rules[str(home / "Documents" / "taxes")] == "deny"
    assert str(home / "Documents") not in rules, "closing it would hide the workspace from its own path"


def test_plan_mode_only_reads(home, tmp_path) -> None:
    rules = _rules(home, tmp_path / "repo", Mode.PLAN)

    assert rules[str((tmp_path / "repo").resolve())] == "read"
    assert rules[str(home / ".cache")] == "read"


def test_a_protected_path_stays_closed_even_inside_something_open(home, tmp_path) -> None:
    secret = str(home / ".cache" / "token")

    rules = _rules(home, tmp_path / "repo", protected=[secret, "~/.north"])

    assert rules[secret] == "deny"
    assert rules[str(Path.home() / ".north")] == "deny"


def test_a_protected_path_wins_over_an_open_rule_for_the_same_path(home, tmp_path) -> None:
    open_toolchain = str(home / ".local" / "bin")

    rules = _rules(home, tmp_path / "repo", protected=[open_toolchain])

    assert rules[open_toolchain] == "deny"


def test_other_runs_copies_are_closed_to_this_one(home, tmp_path) -> None:
    assert _rules(home, tmp_path / "repo")[str(home / COPIES_DIR)] == "deny"


def _linked_copy(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    (repo / "a.py").write_text("x=1\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "i"]):
        subprocess.run([*git, *args], cwd=repo, check=True)
    copy = tmp_path / "copy"
    subprocess.run([*git, "worktree", "add", "-q", "-b", "north/wt", str(copy)], cwd=repo, check=True)
    return repo, copy


def test_a_linked_copy_cannot_have_its_git_link_removed_or_repointed(home, tmp_path) -> None:
    repo, copy = _linked_copy(tmp_path)

    rules = _rules(home, copy)

    assert rules[str((copy / ".git").resolve())] == "read", "deleting or repointing it breaks the copy"
    assert rules[str((repo / ".git").resolve())] == "read", "history and objects: readable, never writable"
    admin = next(path for path, access in rules.items() if "worktrees" in path and access == "write")
    assert Path(admin).parent.parent == (repo / ".git").resolve(), "the copy's own index is the one writable git folder"


def test_in_plan_mode_the_copys_git_folder_is_not_writable(home, tmp_path) -> None:
    _, copy = _linked_copy(tmp_path)

    assert "write" not in {a for p, a in _rules(home, copy, Mode.PLAN).items() if "worktrees" in p}


def test_the_profile_is_the_base_with_these_rules() -> None:
    config = profile(":workspace", "northworker", {"/a": "deny"})

    assert config == {
        "default_permissions": "northworker",
        "permissions": {"northworker": {"extends": ":workspace", "filesystem": {"/a": "deny"}}},
    }
