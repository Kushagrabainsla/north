"""Tests for find_references multi-language search (review finding R5#30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.models import ToolInput
from tools.semantic import find_references as fr
from tools.semantic.find_references import FindReferencesTool


@pytest.fixture
def python_engine(monkeypatch):
    """Force the Python fallback engine for deterministic results."""
    import tools.universal.search_files as sf

    monkeypatch.setattr(sf, "_rg_binary", lambda: None)


async def test_finds_references_across_languages(python_engine, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("widget = make_widget()\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("const widget = makeWidget();\n", encoding="utf-8")
    (tmp_path / "c.go").write_text("widget := NewWidget()\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("widget mentioned in prose\n", encoding="utf-8")

    result = await FindReferencesTool().run(ToolInput(params={"symbol": "widget", "path": str(tmp_path)}))
    assert result.success is True
    files = {Path(r["file"]).suffix for r in result.data["references"]}
    assert {".py", ".ts", ".go"} <= files
    # A non-source extension is not searched in directory mode.
    assert ".txt" not in files


async def test_unsupported_single_file_is_explicit_error(python_engine, tmp_path: Path) -> None:
    """A 0-result on an unsupported file would be falsely reassuring (R5#30)."""
    target = tmp_path / "data.txt"
    target.write_text("widget\n", encoding="utf-8")

    result = await FindReferencesTool().run(ToolInput(params={"symbol": "widget", "path": str(target)}))
    assert result.success is False
    assert "does not support" in result.error


async def test_supported_single_file_searches(python_engine, tmp_path: Path) -> None:
    target = tmp_path / "main.go"
    target.write_text("func main() { widget() }\n", encoding="utf-8")

    result = await FindReferencesTool().run(ToolInput(params={"symbol": "widget", "path": str(target)}))
    assert result.success is True
    assert result.data["total"] == 1


def test_source_globs_cover_common_languages() -> None:
    suffixes = {g.lstrip("*") for g in fr.SOURCE_GLOBS}
    assert {".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java"} <= suffixes
