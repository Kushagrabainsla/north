"""Tests for search_files hardening: sensitive-dir pruning and bounded fallback
(review findings R4#22, R4#23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.models import ToolInput
from tools.universal import search_files as sf
from tools.universal.search_files import SearchFilesTool


@pytest.fixture
def python_engine(monkeypatch):
    """Force the pure-Python fallback so tests are deterministic across machines."""
    monkeypatch.setattr(sf, "_rg_binary", lambda: None)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("API_MARKER = 'in source'\n", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("API_MARKER private key\n", encoding="utf-8")
    (tmp_path / ".north").mkdir()
    (tmp_path / ".north" / "creds.env").write_text("API_MARKER=topsecret\n", encoding="utf-8")
    return tmp_path


async def test_python_walker_skips_sensitive_dirs(python_engine, workspace: Path) -> None:
    result = await SearchFilesTool().run(ToolInput(params={"pattern": "API_MARKER", "path": str(workspace)}))
    assert result.success is True
    files = {m["file"] for m in result.data["matches"]}
    assert any("app.py" in f for f in files)
    assert not any(".ssh" in f or ".north" in f for f in files), files


async def test_direct_file_target_in_sensitive_dir_denied(python_engine, workspace: Path) -> None:
    result = await SearchFilesTool().run(
        ToolInput(params={"pattern": "API_MARKER", "path": ".ssh/id_rsa", "workspace": str(workspace)})
    )
    # resolve_path itself denies sensitive names; either way no content leaks.
    matches = (result.data or {}).get("matches", [])
    assert not matches


async def test_ripgrep_command_excludes_sensitive_dirs() -> None:
    cmd = sf._build_rg_command(
        "rg", Path("/tmp"), "x", sf.SearchOptions(globs=("*",), mode="content", context=0, limit=10)
    )
    joined = " ".join(cmd)
    for name in (".ssh", ".north", ".aws", ".gnupg"):
        assert f"!**/{name}/**" in joined


async def test_long_lines_are_truncated_before_matching(python_engine, tmp_path: Path) -> None:
    """ReDoS mitigation: regex never sees more than _MAX_LINE_CHARS per line."""
    early = "NEEDLE" + "x" * 100
    late = "y" * (sf._MAX_LINE_CHARS + 10) + "NEEDLE"
    (tmp_path / "data.txt").write_text(early + "\n" + late + "\n", encoding="utf-8")

    result = await SearchFilesTool().run(ToolInput(params={"pattern": "NEEDLE", "path": str(tmp_path)}))
    assert result.success is True
    assert len(result.data["matches"]) == 1
    assert result.data["matches"][0]["line"] == 1


async def test_fallback_times_out_instead_of_wedging(python_engine, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("hello\n" * 10, encoding="utf-8")
    monkeypatch.setattr(sf, "_PY_TIMEOUT_SECONDS", -1.0)  # deadline already passed

    result = await SearchFilesTool().run(ToolInput(params={"pattern": "hello", "path": str(tmp_path)}))
    assert result.success is False
    assert "timed out" in result.error
