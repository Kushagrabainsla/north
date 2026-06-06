"""Atomic fact store for personal context, backed by SQLite + per-entry embeddings.

Each extracted fact is stored as one row, embedded individually, and retrieved
by cosine similarity at agent load time. This replaces the flat-markdown "load
everything" approach with targeted semantic retrieval (~15 facts vs. full docs).

The markdown files in FileContextStore remain as a human-readable mirror — facts
are written to both, so the web UI and existing backup/trim logic stay intact.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection
from utils.ids import generate_id

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_facts (
    id          TEXT     NOT NULL PRIMARY KEY,
    content     TEXT     NOT NULL,
    category    TEXT     NOT NULL DEFAULT 'public',
    embedding   TEXT,
    updated_at  DATETIME NOT NULL
)
"""

_MAX_FACTS_RETURNED: int = 15


def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np
    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_SCHEMA)
        # (id, content, embedding_vector) — rebuilt lazily, invalidated on insert.
        self._cache: list[tuple[str, str, list[float]]] | None = None

    async def add_fact(self, content: str, category: str = "public") -> None:
        """Embed and persist one fact. Silently skips empty content or embed failure."""
        content = content.strip()
        if not content:
            return
        try:
            embeddings = await self._embed_fn([content])
            emb_json = json.dumps(embeddings[0]) if embeddings else json.dumps([])
        except Exception:
            logger.warning("FactStore: embed failed — storing fact without embedding")
            emb_json = json.dumps([])
        await asyncio.to_thread(self._insert_sync, content, category, emb_json)
        self._cache = None

    async def search(self, query: str, max_results: int = _MAX_FACTS_RETURNED) -> list[str]:
        """Return up to max_results fact strings most semantically similar to query.

        Falls back to recency order when embeddings are unavailable.
        """
        try:
            q_embs = await self._embed_fn([query])
        except Exception:
            return await asyncio.to_thread(self._recent_facts_sync, max_results)
        if not q_embs:
            return await asyncio.to_thread(self._recent_facts_sync, max_results)
        qvec = q_embs[0]

        if self._cache is None:
            await self._rebuild_cache()

        if not self._cache:
            return []

        scored = [
            (content, _cosine(qvec, emb))
            for _, content, emb in self._cache
            if emb
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [content for content, _ in scored[:max_results]]

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    async def all_facts(self, category: str | None = None) -> list[dict]:
        """Return all facts for web UI display, optionally filtered by category."""
        return await asyncio.to_thread(self._all_facts_sync, category)

    def invalidate_cache(self) -> None:
        self._cache = None

    async def _rebuild_cache(self) -> None:
        rows = await asyncio.to_thread(self._load_all_sync)
        parsed: list[tuple[str, str, list[float]]] = []
        for row_id, content, emb_json in rows:
            if emb_json:
                try:
                    emb = json.loads(emb_json)
                    if emb:
                        parsed.append((row_id, content, emb))
                except (json.JSONDecodeError, ValueError):
                    pass
        self._cache = parsed

    def _insert_sync(self, content: str, category: str, emb_json: str) -> None:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO context_facts (id, content, category, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (generate_id(), content, category, emb_json, now),
            )

    def _load_all_sync(self) -> list[tuple[str, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, content, embedding FROM context_facts"
            ).fetchall()
        return [(r["id"], r["content"], r["embedding"] or "") for r in rows]

    def _recent_facts_sync(self, limit: int) -> list[str]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT content FROM context_facts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["content"] for r in rows]

    def _count_sync(self) -> int:
        with open_db_connection(self._db_path) as conn:
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
                    "SELECT id, content, category, updated_at FROM context_facts "
                    "ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]
