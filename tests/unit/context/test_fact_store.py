"""Tests for FactStore bounding and cache-rebuild safety (review findings R4#24, R4#26)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from context import fact_store as fact_store_module
from context.fact_store import FactStore


def _embedder(vectors: dict[str, list[float]] | None = None, default: list[float] | None = None):
    async def embed(texts: list[str]) -> list[list[float]]:
        return [(vectors or {}).get(t, default if default is not None else []) for t in texts]

    return embed


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "facts.db"


async def test_store_is_capped(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fact_store_module, "_MAX_FACTS_STORED", 5)
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))

    for i in range(9):
        await store.add_fact(f"fact number {i}")

    assert await store.count() == 5
    # The survivors are the most recent ones.
    recent = await store.all_facts()
    assert {r["content"] for r in recent} == {f"fact number {i}" for i in range(4, 9)}


async def test_dedup_updates_in_place_within_scan_window(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[1.0, 0.0]))
    await store.add_fact("first")
    # Identical embedding → dedup fires and updates in place instead of inserting.
    await store.add_fact("first again")
    assert await store.count() == 1


async def test_dedup_only_scans_recent_rows(db_path: Path, monkeypatch) -> None:
    """An old near-duplicate outside the scan window no longer blocks an insert —
    the dedup scan is bounded instead of O(all rows)."""
    monkeypatch.setattr(fact_store_module, "_DEDUP_SCAN_LIMIT", 1)
    vectors = {
        "oldest": [1.0, 0.0],
        "newer": [0.0, 1.0],
        "dup of oldest": [1.0, 0.0],
    }
    store = FactStore(db_path=db_path, embed_fn=_embedder(vectors=vectors))
    await store.add_fact("oldest")
    await store.add_fact("newer")
    await store.add_fact("dup of oldest")  # window only sees "newer" → inserts
    assert await store.count() == 3


async def test_concurrent_searches_rebuild_cache_once(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[1.0, 0.0]))
    await store.add_fact("the sky is blue")
    store.invalidate_cache()

    rebuilds = 0
    original = store._load_all_sync

    def counting_load():
        nonlocal rebuilds
        rebuilds += 1
        return original()

    store._load_all_sync = counting_load  # type: ignore[method-assign]

    results = await asyncio.gather(*[store.search("sky") for _ in range(8)])
    assert all(r == ["the sky is blue"] for r in results)
    assert rebuilds == 1, "concurrent searches must share one cache rebuild"
