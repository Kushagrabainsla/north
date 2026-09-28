"""A delegated run's granted folder can only narrow."""

from __future__ import annotations

from pathlib import Path

from utils.edit_scope import narrow_workspace


def test_inherits_the_grant_when_nothing_is_requested(tmp_path: Path) -> None:
    assert narrow_workspace(str(tmp_path), "") == str(tmp_path)


def test_narrows_to_a_subfolder(tmp_path: Path) -> None:
    assert narrow_workspace(str(tmp_path), str(tmp_path / "pkg")) == str(tmp_path / "pkg")


def test_refuses_a_parent_folder(tmp_path: Path) -> None:
    assert narrow_workspace(str(tmp_path / "pkg"), str(tmp_path)) == str(tmp_path / "pkg")


def test_refuses_an_escape_through_dot_dot(tmp_path: Path) -> None:
    assert narrow_workspace(str(tmp_path / "pkg"), str(tmp_path / "pkg" / ".." / "other")) == str(tmp_path / "pkg")


def test_nothing_granted_passes_nothing_on(tmp_path: Path) -> None:
    assert narrow_workspace("", str(tmp_path)) == ""
