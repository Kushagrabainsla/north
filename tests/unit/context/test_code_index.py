"""Tests for CodeIndex (#2 code RAG).

Uses a deterministic bag-of-words fake embedder so retrieval ordering is
reproducible without any network / real embedding model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from context.code_index import CodeIndex

_VOCAB = [
    "add",
    "sum",
    "subtract",
    "multiply",
    "config",
    "settings",
    "validate",
    "retry",
    "backoff",
    "user",
    "auth",
    "number",
]


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[float(t.lower().count(w)) for w in _VOCAB] for t in texts]


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_search_returns_semantically_relevant_symbol(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef validate_config(settings):\n    return bool(settings)\n",
    )
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)

    results = await idx.search(str(tmp_path), "validate the config settings", max_results=1)
    assert results
    _path, _start, _end, symbol, _code = results[0]
    assert "validate_config" in symbol


@pytest.mark.asyncio
async def test_methods_and_class_card_are_chunked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "svc.py",
        "class Calculator:\n"
        '    """Does math."""\n'
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "    def subtract(self, a, b):\n"
        "        return a - b\n",
    )
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    # Query for a specific method resolves to that method chunk.
    res = await idx.search(str(tmp_path), "subtract two numbers", max_results=1)
    assert res and "subtract" in res[0][3]


@pytest.mark.asyncio
async def test_incremental_reindex_on_file_change(tmp_path: Path) -> None:
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    _write(tmp_path, "m.py", "def add(a, b):\n    return a + b\n")
    res1 = await idx.search(str(tmp_path), "add", max_results=5)
    assert any("add" in r[3] for r in res1)

    # Replace the function; the changed hash must trigger a re-embed.
    _write(tmp_path, "m.py", "def retry(fn):\n    return fn\n")
    res2 = await idx.search(str(tmp_path), "retry backoff", max_results=5)
    assert any("retry" in r[3] for r in res2)
    assert not any("add" in r[3] for r in res2)


@pytest.mark.asyncio
async def test_deleted_file_is_pruned(tmp_path: Path) -> None:
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    _write(tmp_path, "a.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path, "b.py", "def validate_config(s):\n    return s\n")
    await idx.search(str(tmp_path), "add", max_results=5)

    (tmp_path / "b.py").unlink()
    res = await idx.search(str(tmp_path), "validate config", max_results=5)
    assert not any(r[0] == "b.py" for r in res)


@pytest.mark.asyncio
async def test_non_python_files_use_window_chunks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "handler.go",
        "func RetryWithBackoff() {\n    // retry backoff loop\n}\n",
    )
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    res = await idx.search(str(tmp_path), "retry backoff", max_results=1)
    assert res
    assert res[0][0] == "handler.go"


@pytest.mark.asyncio
async def test_embed_failure_returns_empty(tmp_path: Path) -> None:
    async def boom(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embeddings down")

    _write(tmp_path, "m.py", "def add(a, b):\n    return a + b\n")
    idx = CodeIndex(tmp_path / "idx.db", boom)
    res = await idx.search(str(tmp_path), "add", max_results=5)
    assert res == []


@pytest.mark.asyncio
async def test_missing_workspace_returns_empty(tmp_path: Path) -> None:
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    res = await idx.search(str(tmp_path / "does_not_exist"), "anything", max_results=5)
    assert res == []
