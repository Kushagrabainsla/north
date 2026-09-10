"""Tests for the platform-owned filesystem traversal constant.

These assert direct ownership by ``utils.filesystem`` (the pure ``PRUNED_DIRS``
extraction) and that both ``tools._path`` and the orchestrator commit path see
the *same* object, so the file-walking tools and the commit path can never drift
apart on what to prune.
"""

from __future__ import annotations

from utils.filesystem import PRUNED_DIRS


class TestPrunedDirsValue:
    def test_is_frozenset(self) -> None:
        assert isinstance(PRUNED_DIRS, frozenset)

    def test_expected_members(self) -> None:
        assert (
            frozenset(
                {
                    ".git",
                    "node_modules",
                    "__pycache__",
                    ".venv",
                    "venv",
                    ".ruff_cache",
                    ".pytest_cache",
                    "build",
                    "dist",
                }
            )
            == PRUNED_DIRS
        )


class TestToolsPathReExportsSameObject:
    """tools._path must re-export the identical object (compat + shared source)."""

    def test_reexport_is_identical(self) -> None:
        from tools import _path

        assert _path.PRUNED_DIRS is PRUNED_DIRS


class TestCommitUsesSamePlatformObject:
    """The orchestrator commit path shares the one platform constant."""

    def test_commit_pruned_dirs_is_identical(self) -> None:
        from orchestrator import commit

        assert commit.PRUNED_DIRS is PRUNED_DIRS

    def test_commit_and_tools_path_agree(self) -> None:
        from orchestrator import commit
        from tools import _path

        assert commit.PRUNED_DIRS is _path.PRUNED_DIRS
