"""Atomic fact store for personal context, backed by SQLite + per-entry embeddings.

Each extracted fact is stored as one row, embedded individually, and retrieved
by cosine similarity at agent load time. This replaces the flat-markdown "load
everything" approach with targeted semantic retrieval (~15 facts vs. full docs).

The markdown files in FileContextStore remain as a human-readable mirror - facts
are written to both, so the web UI and existing backup/trim logic stay intact.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from config.dependencies import EmbedFn
from utils.db import open_db_connection
from utils.ids import generate_id
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_facts (
    id          TEXT     NOT NULL PRIMARY KEY,
    content     TEXT     NOT NULL,
    category    TEXT     NOT NULL DEFAULT 'user',
    embedding   TEXT,
    updated_at  DATETIME NOT NULL
)
"""

_MAX_FACTS_RETURNED: int = 15
_DEDUP_SIMILARITY_THRESHOLD: float = 0.85
# Recalled facts below this cosine similarity to the query are dropped as noise (#10).
_RECALL_MIN_SIMILARITY: float = 0.25
# Retention cap: the store holds at most this many facts (oldest evicted on
# insert), which also bounds every cosine scan and the in-memory cache.
_MAX_FACTS_STORED: int = 5_000
# Dedup-on-insert only compares against the most recent rows per category  -
# an O(all rows) scan per insert does not scale and recent facts are the
# plausible duplicates anyway.
_DEDUP_SCAN_LIMIT: int = 500


class FactStore:
    """Per-fact storage with per-entry embeddings for semantic context injection.

    Extractions write atomic facts here (one sentence each). Context injection
    queries by cosine similarity instead of loading entire markdown documents.
    Falls back to recency ordering when embedding is unavailable.
    """

    def __init__(self, db_path: Path, embed_fn: EmbedFn) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
        # (id, content, embedding_vector, category) - rebuilt lazily, invalidated on insert.
        self._cache: list[tuple[str, str, list[float], str]] | None = None
        # Serializes cache rebuilds: concurrent searches after an invalidation
        # must not interleave loads and clobber each other's cache.
        self._cache_lock = asyncio.Lock()

    async def add_fact(self, content: str, category: str = "user") -> bool:
        """Embed and persist one fact. Returns True when a new row was inserted.

        Dedup is two-layered and lives entirely in the store (bootstrap and the
        normal pipeline share this exact path):

        1. Exact match — deterministic, needs no embeddings. If an identical
           fact already exists in the same category, its ``updated_at`` is
           refreshed (recency bump) and False is returned. This is the defense
           that keeps identical facts from flooding the store even while the
           embedding provider is rate-limited or down (e.g. 429s).
        2. Cosine similarity — semantic. When an embedding was produced and a
           recent fact in the same category scores >= _DEDUP_SIMILARITY_THRESHOLD,
           the existing row is updated in-place rather than a duplicate being
           inserted (catches paraphrases the exact check misses).

        Silently skips empty content.
        """
        content = content.strip()
        if not content:
            return False
        # Exact-match dedup runs BEFORE any embedding call: identical content
        # is a duplicate by definition, and skipping the embed for it saves
        # rate-limit budget as well as rows.
        if await asyncio.to_thread(self._find_exact_sync, category, content):
            await asyncio.to_thread(self._touch_sync, category, content)
            return False
        new_emb: list[float] = []
        try:
            embeddings = await self._embed_fn([content])
            new_emb = embeddings[0] if embeddings else []
            emb_json = json.dumps(new_emb) if new_emb else json.dumps([])
        except Exception:
            logger.warning("FactStore: embed failed - storing fact without embedding")
            emb_json = json.dumps([])

        replace_id: str | None = None
        if new_emb:
            replace_id = await asyncio.to_thread(
                self._find_similar_sync, category, new_emb, _DEDUP_SIMILARITY_THRESHOLD
            )

        inserted = await asyncio.to_thread(self._insert_or_replace_sync, content, category, emb_json, replace_id)
        self._cache = None
        return inserted

    async def search(
        self,
        query: str,
        max_results: int = _MAX_FACTS_RETURNED,
        allowed_categories: frozenset[str] | None = None,
    ) -> list[str]:
        """Return up to max_results fact strings most semantically similar to query.

        When *allowed_categories* is given, only facts in those categories are
        considered, so a caller never receives a fact it is not permitted to
        read. Falls back to recency order when embeddings are unavailable.
        """
        try:
            q_embs = await self._embed_fn([query])
        except Exception:
            return await asyncio.to_thread(self._recent_facts_sync, max_results, allowed_categories)
        # Empty query vectors (rate-limited embedder returning degenerate
        # results) mean semantic ranking is impossible — fall back to recency.
        if not q_embs or not q_embs[0]:
            return await asyncio.to_thread(self._recent_facts_sync, max_results, allowed_categories)
        qvec = q_embs[0]

        cache = await self._get_cache()
        if not cache:
            # No stored fact has a usable embedding (they were written while
            # the embedder was down): cosine ranking would match nothing.
            # Return the most recent facts so recall still answers.
            return await asyncio.to_thread(self._recent_facts_sync, max_results, allowed_categories)

        scored = [
            (content, cosine_similarity(qvec, emb))
            for _, content, emb, category in cache
            if emb and (allowed_categories is None or category in allowed_categories)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        # Drop weak matches so irrelevant facts never dilute the agent's context (#10).
        return [content for content, score in scored[:max_results] if score >= _RECALL_MIN_SIMILARITY]

    async def count(self, category: str | None = None) -> int:
        """Total facts, or only those in *category* when given."""
        return await asyncio.to_thread(self._count_sync, category)

    async def all_facts(self, category: str | None = None) -> list[dict]:
        """Return all facts for web UI display, optionally filtered by category."""
        return await asyncio.to_thread(self._all_facts_sync, category)

    def invalidate_cache(self) -> None:
        self._cache = None

    async def _get_cache(self) -> list[tuple[str, str, list[float], str]]:
        """Return the embedding cache, rebuilding it at most once concurrently.

        The lock prevents the rebuild race: two coroutines that both observe an
        invalidated cache would otherwise interleave loads and swap in stale or
        duplicated data. The rebuild is built into a local list and swapped in
        atomically (single assignment) once complete.
        """
        cache = self._cache
        if cache is not None:
            return cache
        async with self._cache_lock:
            if self._cache is None:
                rows = await asyncio.to_thread(self._load_all_sync)
                parsed: list[tuple[str, str, list[float], str]] = []
                for row_id, content, emb_json, category in rows:
                    if emb_json:
                        try:
                            emb = json.loads(emb_json)
                            if emb:
                                parsed.append((row_id, content, emb, category))
                        except (json.JSONDecodeError, ValueError):
                            pass
                self._cache = parsed
            return self._cache

    def _find_exact_sync(self, category: str, content: str) -> bool:
        """True if an identical fact already exists in *category*."""
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM context_facts WHERE category = ? AND content = ? LIMIT 1",
                (category, content),
            ).fetchone()
        return row is not None

    def _touch_sync(self, category: str, content: str) -> None:
        """Refresh recency of an existing fact (exact-match dedup path)."""
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE context_facts SET updated_at = ? WHERE category = ? AND content = ?",
                (datetime.now(UTC).isoformat(), category, content),
            )

    def _find_similar_sync(self, category: str, emb: list[float], threshold: float) -> str | None:
        """Return the id of a recent fact in *category* with similarity >= threshold, or None.

        Bounded to the most recent _DEDUP_SCAN_LIMIT rows so insert cost does
        not grow with total store size.
        """
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM context_facts WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, _DEDUP_SCAN_LIMIT),
            ).fetchall()
        for row in rows:
            if not row["embedding"]:
                continue
            try:
                existing_emb = json.loads(row["embedding"])
                if existing_emb and cosine_similarity(emb, existing_emb) >= threshold:
                    return row["id"]
            except (json.JSONDecodeError, ValueError):
                pass
        return None

    def _insert_or_replace_sync(self, content: str, category: str, emb_json: str, replace_id: str | None) -> bool:
        """Insert *content*; returns True when a new row was created.

        When *replace_id* is set the existing row is updated in place (cosine
        dedup path) and False is returned.
        """
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            if replace_id:
                conn.execute(
                    "UPDATE context_facts SET content = ?, embedding = ?, updated_at = ? WHERE id = ?",
                    (content, emb_json, now, replace_id),
                )
                return False
            conn.execute(
                "INSERT INTO context_facts (id, content, category, embedding, updated_at) VALUES (?, ?, ?, ?, ?)",
                (generate_id(), content, category, emb_json, now),
            )
            # Retention: evict the oldest facts beyond the cap so the store
            # (and every scan over it) stays bounded.
            conn.execute(
                "DELETE FROM context_facts WHERE id NOT IN "
                "(SELECT id FROM context_facts ORDER BY updated_at DESC LIMIT ?)",
                (_MAX_FACTS_STORED,),
            )
            return True

    def _load_all_sync(self) -> list[tuple[str, str, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT id, content, embedding, category FROM context_facts").fetchall()
        return [(r["id"], r["content"], r["embedding"] or "", r["category"]) for r in rows]

    def _recent_facts_sync(self, limit: int, allowed_categories: frozenset[str] | None = None) -> list[str]:
        if allowed_categories is not None and not allowed_categories:
            return []
        with open_db_connection(self._db_path) as conn:
            if allowed_categories is not None:
                placeholders = ",".join("?" * len(allowed_categories))
                rows = conn.execute(
                    f"SELECT content FROM context_facts WHERE category IN ({placeholders}) "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (*allowed_categories, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM context_facts ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [r["content"] for r in rows]

    def _count_sync(self, category: str | None = None) -> int:
        with open_db_connection(self._db_path) as conn:
            if category:
                return conn.execute("SELECT COUNT(*) FROM context_facts WHERE category = ?", (category,)).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM context_facts").fetchone()[0]

    def _all_facts_sync(self, category: str | None) -> list[dict]:
        with open_db_connection(self._db_path) as conn:
            if category:
                rows = conn.execute(
                    "SELECT id, content, category, updated_at FROM context_facts "
                    "WHERE category = ? ORDER BY updated_at DESC",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, category, updated_at FROM context_facts ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]
