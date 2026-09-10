"""Tests for the platform-owned per-task handoff directory helpers.

These assert direct ownership by ``utils.handoff`` (Stage 4 extraction) and that
``tools._path`` still re-exports the *same* objects so existing importers and the
sensitive-path carve-out stay anchored to one cached root.
"""

from __future__ import annotations

import time
from pathlib import Path

from utils.handoff import (
    _handoff_root,
    ensure_handoff_dir,
    handoff_dir_for,
    prune_handoff_dirs,
)


class TestHandoffRootHonorsNorthHome:
    """The root is derived from NORTH_HOME (the autouse fixture points it at tmp)."""

    def test_root_is_tasks_under_north_home(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        assert _handoff_root() == str((tmp_path / "tasks").resolve())

    def test_root_is_cached(self, monkeypatch, tmp_path: Path) -> None:
        """lru_cache means a later env change is not seen until cache_clear()."""
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        first = _handoff_root()
        monkeypatch.setenv("NORTH_HOME", str(tmp_path / "other"))
        assert _handoff_root() == first  # still cached
        _handoff_root.cache_clear()
        assert _handoff_root() == str((tmp_path / "other" / "tasks").resolve())


class TestHandoffDirFor:
    def test_dir_for_is_task_under_root(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        assert handoff_dir_for("t1") == f"{(tmp_path / 'tasks').resolve()}/t1"


class TestEnsureHandoffDir:
    def test_creates_directory_and_returns_path(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        path = ensure_handoff_dir("t1")
        assert Path(path).is_dir()
        assert path == handoff_dir_for("t1")

    def test_idempotent(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        first = ensure_handoff_dir("t1")
        second = ensure_handoff_dir("t1")  # exist_ok - no raise
        assert first == second
        assert Path(second).is_dir()


class TestPruneHandoffDirs:
    def _seed(self, monkeypatch, tmp_path: Path) -> Path:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        return Path(_handoff_root())

    def test_no_root_returns_zero(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NORTH_HOME", str(tmp_path / "missing"))
        _handoff_root.cache_clear()
        assert prune_handoff_dirs(7) == 0

    def test_removes_empty_regardless_of_age(self, monkeypatch, tmp_path: Path) -> None:
        root = self._seed(monkeypatch, tmp_path)
        (root / "empty").mkdir(parents=True)
        assert prune_handoff_dirs(0) == 1  # retention 0 keeps content, empties still go
        assert not (root / "empty").exists()

    def test_keeps_running_tasks(self, monkeypatch, tmp_path: Path) -> None:
        root = self._seed(monkeypatch, tmp_path)
        keep_dir = root / "running"
        keep_dir.mkdir(parents=True)
        assert prune_handoff_dirs(0, keep={"running"}) == 0
        assert keep_dir.exists()

    def test_ages_out_stale_content(self, monkeypatch, tmp_path: Path) -> None:
        root = self._seed(monkeypatch, tmp_path)
        stale = root / "stale"
        stale.mkdir(parents=True)
        artifact = stale / "note.md"
        artifact.write_text("old", encoding="utf-8")
        old = time.time() - 30 * 86_400
        import os

        os.utime(artifact, (old, old))
        assert prune_handoff_dirs(7) == 1
        assert not stale.exists()

    def test_keeps_fresh_content(self, monkeypatch, tmp_path: Path) -> None:
        root = self._seed(monkeypatch, tmp_path)
        fresh = root / "fresh"
        fresh.mkdir(parents=True)
        (fresh / "note.md").write_text("new", encoding="utf-8")
        assert prune_handoff_dirs(7) == 0
        assert fresh.exists()

    def test_retention_zero_keeps_content(self, monkeypatch, tmp_path: Path) -> None:
        root = self._seed(monkeypatch, tmp_path)
        with_content = root / "task"
        with_content.mkdir(parents=True)
        artifact = with_content / "note.md"
        artifact.write_text("data", encoding="utf-8")
        old = time.time() - 365 * 86_400
        import os

        os.utime(artifact, (old, old))
        assert prune_handoff_dirs(0) == 0  # 0 = keep everything with content
        assert with_content.exists()


class TestToolsPathReExportsSameObjects:
    """tools._path must re-export the identical objects (compat + shared cache)."""

    def test_reexports_are_identical(self) -> None:
        from tools import _path

        assert _path._handoff_root is _handoff_root
        assert _path.handoff_dir_for is handoff_dir_for
        assert _path.ensure_handoff_dir is ensure_handoff_dir
        assert _path.prune_handoff_dirs is prune_handoff_dirs

    def test_carveout_uses_shared_root(self, monkeypatch, tmp_path: Path) -> None:
        """The sensitive-path gate's handoff carve-out tracks the same cache."""
        from tools import _path

        monkeypatch.setenv("NORTH_HOME", str(tmp_path))
        _handoff_root.cache_clear()
        _path._resolved_blocked_prefixes.cache_clear()
        handoff_file = f"{handoff_dir_for('t1')}/spec.md"
        # Inside the carve-out a plain artifact resolves (not blocked)...
        assert _path.resolve_path(handoff_file, None) is not None
        # ...but a DB file in the same subtree stays blocked.
        assert _path.resolve_path(f"{handoff_dir_for('t1')}/ctx.db", None) is None
