"""SQLite-backed embedding index for semantic context search.

Stores per-paragraph embeddings for all five context documents and exposes
cosine-similarity search.  Falls back silently to an empty result set when
the embed function raises (e.g. OpenRouter is unreachable), so the caller
can fall back to keyword search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_embeddings (
    doc        TEXT    NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    chunk_text TEXT    NOT NULL,
    embedding  TEXT    NOT NULL,
    PRIMARY KEY (doc, chunk_idx)
)
"""

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np  # already a project dependency

    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]


class EmbeddingIndex:
    """Per-document paragraph embeddings stored in SQLite, searched by cosine similarity.

    Embeddings are cached in memory after the first load so that repeated
    searches don't pay the cost of a full SQLite read every time.  The cache
    is invalidated per-document whenever ``update_document`` is called.
    """

    def __init__(self, db_path: Path, embed_fn: EmbedFn) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
        # (doc, chunk_text, embedding_vector) — rebuilt lazily, invalidated on update.
        self._cache: list[tuple[str, str, list[float]]] | None = None

    async def update_document(self, doc_name: str, content: str) -> None:
        """Re-embed all paragraphs for *doc_name* and store them."""
        chunks = _split_paragraphs(content)
        if not chunks:
            await asyncio.to_thread(self._delete_doc_sync, doc_name)
            self._cache = None
            return
        try:
            embeddings = await self._embed_fn(chunks)
        except Exception:
            logger.warning("EmbeddingIndex: embed failed for %s — index not updated", doc_name)
            return
        await asyncio.to_thread(self._write_chunks_sync, doc_name, chunks, embeddings)
        self._cache = None  # invalidate so next search reloads fresh data

    async def search(self, query: str, max_results: int = 5) -> list[tuple[str, str]]:
        """Return up to *max_results* ``(doc_label, chunk_text)`` pairs by similarity.

        Returns an empty list on any error so callers can fall back to keyword search.
        """
        try:
            query_embeddings = await self._embed_fn([query])
        except Exception:
            return []
        if not query_embeddings:
            return []
        qvec = query_embeddings[0]

        if self._cache is None:
            rows = await asyncio.to_thread(self._load_all_sync)
            parsed: list[tuple[str, str, list[float]]] = []
            for doc, chunk_text, emb_json in rows:
                try:
                    emb = json.loads(emb_json)
                except json.JSONDecodeError:
                    continue
                parsed.append((doc, chunk_text, emb))
            self._cache = parsed

        scored: list[tuple[float, str, str]] = []
        for doc, chunk_text, emb in self._cache:
            sim = _cosine(qvec, emb)
            label = doc.removesuffix(".md").replace("_", " ").title()
            scored.append((sim, label, chunk_text))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(label, chunk) for _, label, chunk in scored[:max_results]]

    # ------------------------------------------------------------------ #

    def invalidate_cache(self) -> None:
        """Drop the in-memory embedding cache so the next search reloads from DB."""
        self._cache = None

    def _delete_doc_sync(self, doc_name: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM context_embeddings WHERE doc = ?", (doc_name,))

    def _write_chunks_sync(
        self,
        doc_name: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM context_embeddings WHERE doc = ?", (doc_name,))
            conn.executemany(
                "INSERT INTO context_embeddings (doc, chunk_idx, chunk_text, embedding) VALUES (?, ?, ?, ?)",
                [
                    (doc_name, idx, chunk, json.dumps(emb))
                    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings, strict=False))
                ],
            )

    def _load_all_sync(self) -> list[tuple[str, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT doc, chunk_text, embedding FROM context_embeddings").fetchall()
        return [(r["doc"], r["chunk_text"], r["embedding"]) for r in rows]
