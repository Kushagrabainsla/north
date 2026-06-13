"""Tests for per-workspace mutation locks (review finding R5#33)."""

from __future__ import annotations

from pathlib import Path

from agents.workspace_lock import workspace_lock


def test_same_workspace_shares_one_lock(tmp_path: Path) -> None:
    assert workspace_lock(str(tmp_path)) is workspace_lock(str(tmp_path))


def test_equivalent_paths_share_one_lock(tmp_path: Path) -> None:
    """Two agents naming the same tree differently must still serialize."""
    nested = tmp_path / "repo"
    nested.mkdir()
    assert workspace_lock(str(nested)) is workspace_lock(str(tmp_path / "." / "repo"))


def test_different_workspaces_get_different_locks(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert workspace_lock(str(a)) is not workspace_lock(str(b))


def test_empty_workspace_maps_to_shared_default() -> None:
    assert workspace_lock("") is workspace_lock("")
