"""Tests for CodeIndex (#2 code RAG).

Uses a deterministic bag-of-words fake embedder so retrieval ordering is
reproducible without any network / real embedding model.
"""

from __future__ import annotations

import asyncio
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


def _two_files(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo, "a.py", "def add(a, b):\n    return a + b\n")
    _write(repo, "pkg/b.py", "def validate_config(settings):\n    return bool(settings)\n")  # deeper: indexed second
    return repo


@pytest.mark.asyncio
async def test_a_slow_first_build_does_not_hold_the_search(tmp_path: Path, monkeypatch) -> None:
    """Seaborn's first search waited 6 minutes for the whole index; on-device it is 0.26 s a chunk."""
    monkeypatch.setattr("context.code_index._SEARCH_WAIT_SECONDS", 0.05)
    repo = _two_files(tmp_path)
    second_file = asyncio.Event()

    async def slow(texts: list[str]) -> list[list[float]]:
        if any(text.startswith("pkg/b.py\n") for text in texts):
            await second_file.wait()
        return await _fake_embed(texts)

    idx = CodeIndex(tmp_path / "idx.db", slow)

    first = await idx.search(str(repo), "add numbers", max_results=5)

    assert [path for path, *_ in first] == ["a.py"], "what is done is searched"
    assert idx.building(str(repo)) == (1, 2)
    second_file.set()
    await idx._builds[str(repo.resolve())]
    assert idx.building(str(repo)) is None
    assert {path for path, *_ in await idx.search(str(repo), "config", max_results=5)} == {"a.py", "pkg/b.py"}


@pytest.mark.asyncio
async def test_a_build_that_fails_midway_keeps_the_files_it_did(tmp_path: Path) -> None:
    repo = _two_files(tmp_path)
    down = True

    async def flaky(texts: list[str]) -> list[list[float]]:
        if down and any(text.startswith("pkg/b.py\n") for text in texts):
            raise RuntimeError("embeddings down")
        return await _fake_embed(texts)

    idx = CodeIndex(tmp_path / "idx.db", flaky)

    assert [path for path, *_ in await idx.search(str(repo), "add", max_results=5)] == ["a.py"]
    down = False
    assert {path for path, *_ in await idx.search(str(repo), "add", max_results=5)} == {"a.py", "pkg/b.py"}


@pytest.mark.asyncio
async def test_missing_workspace_returns_empty(tmp_path: Path) -> None:
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    res = await idx.search(str(tmp_path / "does_not_exist"), "anything", max_results=5)
    assert res == []


@pytest.mark.asyncio
async def test_polyglot_syntax_chunks(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "service.ts",
        "export class AuthService {\n  async validateUser() {\n    return true;\n  }\n}\n",
    )
    _write(
        tmp_path,
        "engine.rs",
        "pub async fn compute_sum(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    )
    idx = CodeIndex(tmp_path / "idx.db", _fake_embed)
    res_ts = await idx.search(str(tmp_path), "validate user auth", max_results=1)
    assert res_ts
    assert res_ts[0][0] == "service.ts"
    assert "AuthService" in res_ts[0][3] or "validateUser" in res_ts[0][4]

    res_rs = await idx.search(str(tmp_path), "compute sum add", max_results=1)
    assert res_rs
    assert res_rs[0][0] == "engine.rs"
