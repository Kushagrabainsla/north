"""Tests for the central sensitive-path gate (review findings R1#2, R2#9, R4#23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._path import (
    _handoff_root,
    ensure_handoff_dir,
    find_project_root,
    handoff_dir_for,
    is_sensitive_path,
    references_sensitive_path,
    resolve_path,
)

HOME = Path.home()


class TestResolvePathBlocklist:
    """The blocklist applies in every branch - workspace or not."""

    def test_blocks_ssh_without_workspace(self) -> None:
        assert resolve_path("~/.ssh/id_rsa", None) is None

    def test_blocks_north_home_without_workspace(self) -> None:
        assert resolve_path("~/.north/.env", None) is None
        assert resolve_path("~/.north/secret.key", None) is None

    def test_blocks_etc_without_workspace(self) -> None:
        assert resolve_path("/etc/passwd", None) is None

    def test_blocks_ssh_inside_home_workspace(self) -> None:
        """The R2#9 exploit: workspace=$HOME must not re-open ~/.ssh."""
        assert resolve_path(".ssh/id_rsa", str(HOME)) is None

    def test_blocks_north_inside_home_workspace(self) -> None:
        assert resolve_path(".north/.env", str(HOME)) is None

    def test_blocks_absolute_sensitive_inside_workspace_branch(self) -> None:
        # Path traversal out of the workspace is denied before the blocklist even applies.
        assert resolve_path("../../etc/passwd", str(HOME / "projects")) is None

    def test_allows_normal_file_in_workspace(self, tmp_path: Path) -> None:
        resolved = resolve_path("src/app.py", str(tmp_path))
        assert resolved == tmp_path / "src" / "app.py"

    def test_allows_relative_path_without_workspace(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        resolved = resolve_path("notes.txt", None)
        assert resolved == tmp_path / "notes.txt"

    def test_blocks_secret_key_filename_anywhere(self, tmp_path: Path) -> None:
        assert resolve_path("secret.key", str(tmp_path)) is None


class TestPersonalDataCarveOut:
    """news/notes/wellness subdirs under NORTH_HOME are writable, but the
    rest of ~/.north (secret.key, .env, DBs) stays blocked (R-bootstrap fix).

    Uses explicit tmp_path-based absolute paths so the NORTH_HOME override and
    the path under test stay in sync (``~`` expands to $HOME, not NORTH_HOME).
    """

    def test_allows_news_subdir_write(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        target = tmp_path / "news" / "2026-08-20.md"
        resolved = resolve_path(str(target), None)
        assert resolved == target

    def test_allows_wellness_subdir_write_inside_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Even when the agent runs scoped to a project workspace, personal dirs
        # must be reachable - otherwise output silently falls back to CWD.
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        target = tmp_path / "wellness" / "meal-plans" / "x.md"
        resolved = resolve_path(str(target), "/srv/project")
        assert resolved == target

    def test_blocks_north_home_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The blocked prefix is the *real* ~/.north (~/.ssh/.aws/.config etc.),
        # not an arbitrary NORTH_HOME override. The carve-out must not have
        # opened the real home root or its secrets/DBs.
        monkeypatch.delenv("NORTH_HOME", raising=False)
        assert resolve_path("~/.north/secret.key", None) is None
        assert resolve_path("~/.north/.env", None) is None
        assert resolve_path("~/.north/ledger.db", None) is None
        # The override path's secret is still blocked by the filename policy.
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        assert resolve_path(str(tmp_path / "secret.key"), None) is None

    def test_personal_subdir_readable_not_sensitive(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        assert is_sensitive_path(tmp_path / "news") is False
        assert is_sensitive_path(tmp_path / "notes" / "plan.md") is False
        # Sibling secret stays sensitive even though it bears the .north parent.
        assert is_sensitive_path(tmp_path / "secret.key") is True


class TestReferencesSensitivePath:
    def test_detects_north_env(self) -> None:
        assert references_sensitive_path("cat ~/.north/.env") is True

    def test_detects_ssh(self) -> None:
        assert references_sensitive_path("cat ~/.ssh/id_rsa") is True

    def test_detects_etc(self) -> None:
        assert references_sensitive_path("grep root /etc/passwd") is True

    def test_allows_plain_paths(self) -> None:
        assert references_sensitive_path("cat README.md") is False

    def test_detects_relative_traversal(self) -> None:
        # The instant-safe fast path must not let a relative parent escape read
        # secrets outside the workspace without an approval card (CL1/A1).
        assert references_sensitive_path("cat ../../.ssh/id_rsa") is True
        assert references_sensitive_path("cat ../secret.txt") is True

    def test_allows_relative_subpath(self) -> None:
        assert references_sensitive_path("cat src/app.py") is False


class TestIsSensitivePath:
    def test_sensitive_home_dirs(self) -> None:
        assert is_sensitive_path(HOME / ".ssh" / "id_rsa") is True
        assert is_sensitive_path(HOME / ".north" / "secret.key") is True

    def test_normal_path(self, tmp_path: Path) -> None:
        assert is_sensitive_path(tmp_path / "main.py") is False


class TestFindProjectRoot:
    def test_finds_marker_above_file(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        nested = tmp_path / "src" / "pkg"
        nested.mkdir(parents=True)
        file = nested / "mod.py"
        file.write_text("x = 1\n", encoding="utf-8")
        assert find_project_root(file) == tmp_path

    def test_falls_back_to_parent_without_marker(self, tmp_path: Path) -> None:
        file = tmp_path / "loose.py"
        file.write_text("x = 1\n", encoding="utf-8")
        root = find_project_root(file, markers=("definitely-not-present",))
        assert root == tmp_path


class TestHandoffDirectory:
    """The per-task handoff directory agents are told they can use.

    Agents receive this path in their system context and several of them check it
    before doing anything. Nothing created it - it appeared only as a side effect
    of whichever component happened to write there first - so a pipeline whose
    first step *reads* (the researcher looking for prior context) found it missing
    and stopped the task with "the required handoff directory does not exist".
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Point NORTH_HOME at tmp_path for real.

        ``_handoff_root`` is lru_cached, so setting the env var alone does not
        move it - without clearing the cache these tests write to the developer's
        actual ~/.north.
        """
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        yield
        _handoff_root.cache_clear()

    def test_the_directory_agents_are_promised_actually_exists(self, tmp_path: Path) -> None:
        created = ensure_handoff_dir("task_abc123")
        assert Path(created).is_dir()
        assert created == handoff_dir_for("task_abc123")
        assert Path(created).is_relative_to(tmp_path)  # and it is the isolated one

    def test_creating_it_twice_is_harmless(self) -> None:
        first = ensure_handoff_dir("task_abc123")
        (Path(first) / "research").mkdir()
        assert ensure_handoff_dir("task_abc123") == first
        assert (Path(first) / "research").is_dir()  # nothing already there is disturbed

    def test_it_stays_writable_by_agents(self) -> None:
        """Creating it must not put it outside the handoff carve-out."""
        created = ensure_handoff_dir("task_abc123")
        target = f"{created}/research/context.md"
        assert resolve_path(target, "/srv/project") == Path(target)
