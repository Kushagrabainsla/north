"""Tests for GotoDefinitionTool."""

from __future__ import annotations

from pathlib import Path

from tools.models import ToolInput
from tools.semantic.goto_definition import GotoDefinitionTool


async def test_goto_definition_missing_params():
    tool = GotoDefinitionTool()
    res = await tool.run(ToolInput(params={}))
    assert res.success is False
    assert "path" in res.error.lower()


async def test_goto_definition_nonexistent_file(tmp_path: Path):
    tool = GotoDefinitionTool()
    res = await tool.run(ToolInput(params={"path": str(tmp_path / "nonexistent.py"), "symbol": "foo"}))
    assert res.success is False
    assert "not found" in res.error.lower()


async def test_goto_definition_unsupported_language(tmp_path: Path):
    target = tmp_path / "file.xyz"
    target.write_text("symbol = 1\n", encoding="utf-8")
    tool = GotoDefinitionTool()
    res = await tool.run(ToolInput(params={"path": str(target), "symbol": "symbol"}))
    assert res.success is False
    assert "no language server available" in res.error.lower()


async def test_goto_definition_requires_symbol_or_line(tmp_path: Path):
    target = tmp_path / "file.py"
    target.write_text("def hello(): pass\n", encoding="utf-8")
    tool = GotoDefinitionTool()
    res = await tool.run(ToolInput(params={"path": str(target)}))
    assert res.success is False
    assert "either 'symbol' or 'line'" in res.error.lower()
