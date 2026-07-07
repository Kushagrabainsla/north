"""Tests for RenameSymbolTool (#4). Degradation tests run everywhere; the real
rename test is skipped when pyright-langserver isn't installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.models import ToolInput
from tools.specialized import rename_symbol as rs_mod
from tools.specialized.rename_symbol import RenameSymbolTool

_HAS_PYRIGHT = shutil.which("pyright-langserver") is not None
_needs_pyright = pytest.mark.skipif(not _HAS_PYRIGHT, reason="pyright-langserver not installed")


@pytest.mark.asyncio
async def test_requires_symbol_and_new_name(tmp_path: Path):
    tool = RenameSymbolTool()
    out = await tool.run(ToolInput(params={"path": str(tmp_path / "m.py"), "symbol": "", "new_name": "x"}))
    assert not out.success


@pytest.mark.asyncio
async def test_rejects_invalid_identifier(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    tool = RenameSymbolTool()
    out = await tool.run(
        ToolInput(params={"path": str(f), "symbol": "foo", "new_name": "not a name", "workspace": str(tmp_path)})
    )
    assert not out.success
    assert "identifier" in (out.error or "")


@pytest.mark.asyncio
async def test_missing_file_errors(tmp_path: Path):
    tool = RenameSymbolTool()
    out = await tool.run(
        ToolInput(
            params={"path": str(tmp_path / "nope.py"), "symbol": "a", "new_name": "b", "workspace": str(tmp_path)}
        )
    )
    assert not out.success
    assert "not found" in (out.error or "").lower()


@pytest.mark.asyncio
async def test_no_language_server_degrades_gracefully(tmp_path: Path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(rs_mod, "server_command_for", lambda _suffix: None)
    tool = RenameSymbolTool()
    out = await tool.run(
        ToolInput(params={"path": str(f), "symbol": "foo", "new_name": "bar", "workspace": str(tmp_path)})
    )
    assert not out.success
    assert "language server" in (out.error or "").lower()


@pytest.mark.asyncio
@_needs_pyright
async def test_real_rename_via_tool(tmp_path: Path):
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    f = tmp_path / "calc.py"
    f.write_text("def total(xs):\n    return sum(xs)\n\n\ny = total([1, 2])\n", encoding="utf-8")
    tool = RenameSymbolTool()
    out = await tool.run(
        ToolInput(params={"path": str(f), "symbol": "total", "new_name": "add_all", "workspace": str(tmp_path)})
    )
    assert out.success, out.error
    assert out.data["files_changed"] == 1
    after = f.read_text(encoding="utf-8")
    assert "def add_all(xs):" in after
    assert "y = add_all([1, 2])" in after
