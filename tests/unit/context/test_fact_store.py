"""Tests for FactStore bounding and cache-rebuild safety (review findings R4#24, R4#26)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from memory import facts as fact_store_module
from memory.facts import FactStore


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


async def test_exact_match_dedup_works_without_embeddings(db_path: Path) -> None:
    """Identical content is collapsed even when the embedder is down (429s)."""
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))

    assert await store.add_fact("User bought groceries") is True
    assert await store.add_fact("User bought groceries") is False  # exact dup → no new row
    assert await store.add_fact("User bought groceries", category="bootstrap") is True  # different category

    assert await store.count() == 2
    # The duplicate call did not insert a row.
    assert await store.count(category="user") == 1


async def test_exact_match_dedup_precedes_cosine(db_path: Path) -> None:
    """Exact match wins even when embeddings WOULD have matched: no embed call
    is made for a duplicate, so identical facts never churn rate-limit budget."""
    calls: list[list[str]] = []

    async def counting_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    store = FactStore(db_path=db_path, embed_fn=counting_embed)
    assert await store.add_fact("first") is True
    assert await store.add_fact("first") is False
    assert len(calls) == 1, "exact duplicate must not call the embedder"


async def test_embed_failure_still_stores_distinct_facts(db_path: Path) -> None:
    """Embedder down (raises, like a 429) → distinct facts still store, and
    search falls back to recency order; only exact duplicates are collapsed."""

    async def down_embed(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder rate-limited")

    store = FactStore(db_path=db_path, embed_fn=down_embed)

    await store.add_fact("fact one")
    await store.add_fact("fact two")
    await store.add_fact("fact one")

    assert await store.count() == 2
    recents = await store.search("anything")  # embed raises → recency fallback
    assert set(recents) == {"fact one", "fact two"}


async def test_search_falls_back_when_query_embed_is_empty(db_path: Path) -> None:
    """Embedder returns degenerate empty vectors (not raising) → search still
    returns facts via recency instead of silently returning nothing."""

    async def empty_embed(texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]

    store = FactStore(db_path=db_path, embed_fn=empty_embed)
    await store.add_fact("fact one")
    await store.add_fact("fact two")

    assert set(await store.search("anything")) == {"fact one", "fact two"}


async def test_search_falls_back_when_no_facts_embedded(db_path: Path) -> None:
    """Facts written while the embedder was down have no embeddings; a later
    query with a WORKING embedder must not return [] — recency fallback."""
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("fact one")  # stored without embedding (429 era)

    # Query time: embedder works again, but the store has no embedded facts.
    store._embed_fn = _embedder(default=[1.0, 0.0])  # type: ignore[method-assign]

    assert await store.search("anything") == ["fact one"]


async def test_count_filters_by_category(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("budget fact", category="bootstrap")
    await store.add_fact("another budget fact", category="bootstrap")
    await store.add_fact("learned fact", category="user")
    assert await store.count() == 3
    assert await store.count(category="bootstrap") == 2
    assert await store.count(category="user") == 1
    assert await store.count(category="health") == 0


async def test_dedup_only_scans_recent_rows(db_path: Path, monkeypatch) -> None:
    """An old near-duplicate outside the scan window no longer blocks an insert  -
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


async def test_normalized_dedup_touches_existing_fact_by_id(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    # Add initial fact
    assert await store.add_fact("User is studying at San Jose State") is True
    initial_facts = await store.all_facts()
    initial_id = initial_facts[0]["id"]
    initial_updated = initial_facts[0]["updated_at"]

    # Sleep slightly so timestamp advances
    await asyncio.sleep(0.01)

    # Paraphrased version with filler words
    assert await store.add_fact("The user was studying at the San Jose State") is False
    assert await store.count() == 1

    updated_facts = await store.all_facts()
    assert updated_facts[0]["id"] == initial_id
    assert updated_facts[0]["updated_at"] >= initial_updated
