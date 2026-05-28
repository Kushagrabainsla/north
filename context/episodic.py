"""Episodic memory: per-task summaries with embedding-based retrieval.

After every completed task the Orchestrator records a one-paragraph summary
here.  Before each agent run, ``_load_context`` queries this store for the
top-k most semantically similar past episodes and injects them as context,
making north progressively smarter about your specific patterns and history.

Retrieval falls back to keyword search when the embed function is unavailable
(e.g. tests, offline mode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from utils.db import open_db_connection
from utils.ids import generate_id

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id        TEXT    PRIMARY KEY,
    task_id   TEXT,
    domain    TEXT    NOT NULL,
    summary   TEXT    NOT NULL,
    embedding TEXT,
    timestamp TEXT    NOT NULL
)
"""

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "have",
    "has", "had", "do", "does", "did", "to", "of", "in", "for", "on",
    "with", "at", "by", "from", "and", "or", "but", "not", "i", "my",
    "me", "we", "our", "you", "your", "it", "its",
})

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np

    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _keyword_score(text: str, query_words: frozenset[str]) -> int:
    words = frozenset(
        w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS
    )
    return len(query_words & words)


class EpisodicStore:
    """Stores and retrieves per-task episodic summaries."""

    def __init__(self, db_path: Path, embed_fn: EmbedFn | None = None) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    async def record(self, task_id: str, domain: str, summary: str) -> None:
        """Store a task episode.  Embedding is generated if an embed_fn is available."""
        embedding: list[float] | None = None
        if self._embed_fn is not None:
            try:
                vecs = await self._embed_fn([summary])
                embedding = vecs[0] if vecs else None
            except Exception:
                logger.debug("EpisodicStore: embed failed for task %s", task_id)
        now = datetime.now(timezone.utc).isoformat()
        emb_json = json.dumps(embedding) if embedding is not None else None
        await asyncio.to_thread(
            self._insert_sync,
            generate_id(), task_id, domain, summary, emb_json, now,
        )

    async def search(self, query: str, max_results: int = 3) -> list[str]:
        """Return the most relevant past episode summaries for *query*.

        Tries embedding-based cosine similarity first; falls back to keyword
        overlap scoring so retrieval always works.
        """
        rows = await asyncio.to_thread(self._load_all_sync)
        if not rows:
            return []

        if self._embed_fn is not None:
            try:
                vecs = await self._embed_fn([query])
                if vecs:
                    qvec = vecs[0]
                    scored: list[tuple[float, str]] = []
                    for _id, summary, emb_json in rows:
                        if emb_json is None:
                            continue
                        try:
                            emb = json.loads(emb_json)
                        except json.JSONDecodeError:
                            continue
                        scored.append((_cosine(qvec, emb), summary))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    results = [s for _, s in scored[:max_results] if _ > 0.3]
                    if results:
                        return results
            except Exception:
                pass

        # Keyword fallback
        query_words = frozenset(
            w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOPWORDS
        )
        kw_scored = sorted(
            rows,
            key=lambda r: _keyword_score(r[1], query_words),
            reverse=True,
        )
        return [summary for _, summary, _ in kw_scored[:max_results] if summary]

    # ------------------------------------------------------------------ #

    def _insert_sync(
        self,
        ep_id: str,
        task_id: str,
        domain: str,
        summary: str,
        emb_json: str | None,
        now: str,
    ) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO episodes (id, task_id, domain, summary, embedding, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ep_id, task_id, domain, summary, emb_json, now),
            )

    def _load_all_sync(self) -> list[tuple[str, str, str | None]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, summary, embedding FROM episodes ORDER BY timestamp DESC LIMIT 500"
            ).fetchall()
        return [(r["id"], r["summary"], r["embedding"]) for r in rows]
