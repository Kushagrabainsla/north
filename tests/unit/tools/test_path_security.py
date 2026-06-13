"""Tests for the central sensitive-path gate (review findings R1#2, R2#9, R4#23)."""

from __future__ import annotations

from pathlib import Path

from tools._path import (
    find_project_root,
    is_sensitive_path,
    references_sensitive_path,
    resolve_path,
)

HOME = Path.home()


class TestResolvePathBlocklist:
    """The blocklist applies in every branch — workspace or not."""

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


class TestReferencesSensitivePath:
    def test_detects_north_env(self) -> None:
        assert references_sensitive_path("cat ~/.north/.env") is True

    def test_detects_ssh(self) -> None:
        assert references_sensitive_path("cat ~/.ssh/id_rsa") is True

    def test_detects_etc(self) -> None:
        assert references_sensitive_path("grep root /etc/passwd") is True

    def test_allows_plain_paths(self) -> None:
        assert references_sensitive_path("cat README.md") is False


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
