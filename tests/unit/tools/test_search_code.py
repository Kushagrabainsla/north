"""Tests for SearchCodeTool (#2 code RAG)."""

from __future__ import annotations

from pathlib import Path

import pytest

from context.code_index import CodeIndex
from tools.models import ToolInput
from tools.semantic.search_code import SearchCodeTool

_VOCAB = ["add", "validate", "config", "settings", "retry"]


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[float(t.lower().count(w)) for w in _VOCAB] for t in texts]


@pytest.mark.asyncio
async def test_search_code_returns_matches(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def validate_config(settings):\n    return bool(settings)\n", encoding="utf-8")
    tool = SearchCodeTool(code_index=CodeIndex(tmp_path / "idx.db", _fake_embed))

    out = await tool.run(ToolInput(params={"query": "validate the config", "workspace": str(tmp_path)}))
    assert out.success
    assert out.data["count"] >= 1
    assert any("validate_config" in m["symbol"] for m in out.data["matches"])


@pytest.mark.asyncio
async def test_search_code_requires_query(tmp_path: Path) -> None:
    tool = SearchCodeTool(code_index=CodeIndex(tmp_path / "idx.db", _fake_embed))
    out = await tool.run(ToolInput(params={"workspace": str(tmp_path)}))
    assert not out.success
    assert "query" in (out.error or "")


@pytest.mark.asyncio
async def test_search_code_requires_workspace(tmp_path: Path) -> None:
    tool = SearchCodeTool(code_index=CodeIndex(tmp_path / "idx.db", _fake_embed))
    out = await tool.run(ToolInput(params={"query": "anything"}))
    assert not out.success
    assert "workspace" in (out.error or "")


@pytest.mark.asyncio
async def test_search_code_caps_max_results(tmp_path: Path) -> None:
    body = "\n\n".join(f"def f{i}(config):\n    return config\n" for i in range(30))
    (tmp_path / "many.py").write_text(body, encoding="utf-8")
    tool = SearchCodeTool(code_index=CodeIndex(tmp_path / "idx.db", _fake_embed))

    out = await tool.run(ToolInput(params={"query": "config", "workspace": str(tmp_path), "max_results": 999}))
    assert out.success
    assert out.data["count"] <= 20


@pytest.mark.asyncio
async def test_format_output_lists_locations(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tool = SearchCodeTool(code_index=CodeIndex(tmp_path / "idx.db", _fake_embed))
    out = await tool.run(ToolInput(params={"query": "add", "workspace": str(tmp_path)}))
    text = tool.format_output(out.data)
    assert "m.py" in text
