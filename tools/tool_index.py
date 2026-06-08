"""SQLite-backed embedding index for tool descriptions.

Enables semantic tool selection: at task start, the task prompt is embedded
and the top-K most similar tools are injected instead of the full registry.
Falls back silently (returns []) so callers can fall back to full injection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from config.dependencies import EmbedFn
from utils.db import open_db_connection
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_embeddings (
    name        TEXT     NOT NULL PRIMARY KEY,
    description TEXT     NOT NULL,
    embedding   TEXT     NOT NULL,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SEMANTIC_TOP_K: int = 15  # max tools to inject per task
SEMANTIC_FILTER_MIN: int = 8  # only activate semantic filter when more tools exist than this


class ToolIndex:
    """Embeds tool descriptions for semantic retrieval at agent task start.

    update_tool() is called when a tool is registered.
    search_tools() is called in _load_tools() to get the top-K relevant tools.
    """

    def __init__(self, db_path: Path, embed_fn: EmbedFn) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_SCHEMA)
        # (tool_name, embedding_vector) — rebuilt lazily, invalidated on update.
        self._cache: list[tuple[str, list[float]]] | None = None

    async def update_tool(self, name: str, description: str) -> None:
        """Embed and upsert a tool. Call once per tool at registration time."""
        try:
            embeddings = await self._embed_fn([description])
        except Exception:
            logger.warning("ToolIndex: embed failed for %s — tool not indexed", name)
            return
        if not embeddings:
            return
        emb_json = json.dumps(embeddings[0])
        await asyncio.to_thread(self._upsert_sync, name, description, emb_json)
        self._cache = None

    async def remove_tool(self, name: str) -> None:
        await asyncio.to_thread(self._delete_sync, name)
        self._cache = None

    async def search_tools(self, query: str, top_k: int = SEMANTIC_TOP_K) -> list[str]:
        """Return up to top_k tool names most similar to query.

        Returns [] on any error so callers fall back to full tool injection.
        """
        try:
            q_embs = await self._embed_fn([query])
        except Exception:
            return []
        if not q_embs:
            return []
        qvec = q_embs[0]

        if self._cache is None:
            rows = await asyncio.to_thread(self._load_all_sync)
            parsed: list[tuple[str, list[float]]] = []
            for name, emb_json in rows:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    parsed.append((name, json.loads(emb_json)))
            self._cache = parsed

        scored = [(name, cosine_similarity(qvec, emb)) for name, emb in self._cache]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in scored[:top_k]]

    def invalidate_cache(self) -> None:
        self._cache = None

    def _upsert_sync(self, name: str, description: str, emb_json: str) -> None:
        from utils.time import utcnow

        now = utcnow().isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO tool_embeddings (name, description, embedding, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, "
                "embedding=excluded.embedding, "
                "updated_at=excluded.updated_at",
                (name, description, emb_json, now),
            )

    def _delete_sync(self, name: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM tool_embeddings WHERE name = ?", (name,))

    def _load_all_sync(self) -> list[tuple[str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT name, embedding FROM tool_embeddings").fetchall()
        return [(r["name"], r["embedding"]) for r in rows]
