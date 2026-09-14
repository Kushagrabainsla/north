"""SQLite-backed hybrid index for enriched tool retrieval profiles.

Dense similarity and lexical BM25 rankings are fused at query time. This keeps
semantic matches while recovering exact operational terms that embeddings miss.
Callers fall back to full injection only when neither ranking is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from inference.models import EmbedFn
from tools.retrieval import bm25_rank, reciprocal_rank_fusion
from utils.db import open_db_connection
from utils.math import cosine_similarity
from utils.vector_space import ensure_vector_space

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_embeddings (
    name        TEXT     NOT NULL PRIMARY KEY,
    description TEXT     NOT NULL,
    embedding   TEXT     NOT NULL,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SEMANTIC_TOP_K: int = 8  # benchmarked default: leanest high-recall candidate set
SEMANTIC_FILTER_MIN: int = 8  # only activate semantic filter when more tools exist than this


class ToolIndex:
    """Indexes enriched profiles for hybrid retrieval at agent task start.

    update_tool() is called when a tool is registered.
    search_tools() is called in _load_tools() to get the top-K relevant tools.
    """

    def __init__(self, db_path: Path, embed_fn: EmbedFn, embedding_model: str = "") -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_SCHEMA)
        # A change of embedding model invalidates every stored vector; the tool
        # descriptions they were built from are re-read at every startup.
        ensure_vector_space(self._db_path, embedding_model, ("tool_embeddings",))
        # (tool_name, retrieval_profile, embedding_vector), rebuilt lazily.
        self._cache: list[tuple[str, str, list[float]]] | None = None

    async def update_tool(self, name: str, description: str) -> None:
        """Embed and upsert a tool. Call once per tool at registration time.

        ``description`` is the complete retrieval profile. The legacy name is
        retained because it is also the persisted column name. Skips embedding
        when the stored profile is unchanged -
        the whole registry is re-indexed at every startup, and without this
        each boot would re-embed every tool.
        """
        stored = await asyncio.to_thread(self._get_description_sync, name)
        if stored == description:
            return
        try:
            embeddings = await self._embed_fn([description])
        except Exception:
            logger.warning("ToolIndex: embed failed for %s - tool not indexed", name)
            return
        if not embeddings:
            return
        emb_json = json.dumps(embeddings[0])
        await asyncio.to_thread(self._upsert_sync, name, description, emb_json)
        self._cache = None

    async def update_tools(self, tools: list[tuple[str, str]]) -> int:
        """Index many ``(name, retrieval_profile)`` pairs in one embedding call.

        Startup indexes the whole registry, and doing that one tool at a time was
        one network round trip per tool on a first boot. Unchanged descriptions are
        skipped, so later boots do no embedding work at all. Returns the number of
        tools actually re-embedded.
        """
        stored = await asyncio.to_thread(self._all_descriptions_sync)
        changed = [(name, desc) for name, desc in tools if stored.get(name) != desc]
        if not changed:
            return 0
        try:
            embeddings = await self._embed_fn([desc for _, desc in changed])
        except Exception:
            logger.warning("ToolIndex: batch embed failed - %d tool(s) not indexed", len(changed))
            return 0
        if len(embeddings) != len(changed):
            logger.warning(
                "ToolIndex: embed returned %d vectors for %d tools - skipping batch", len(embeddings), len(changed)
            )
            return 0
        rows = [(name, desc, json.dumps(vec)) for (name, desc), vec in zip(changed, embeddings, strict=True)]
        await asyncio.to_thread(self._upsert_many_sync, rows)
        self._cache = None
        return len(rows)

    async def remove_tool(self, name: str) -> None:
        await asyncio.to_thread(self._delete_sync, name)
        self._cache = None

    async def search_tools(self, query: str, top_k: int = SEMANTIC_TOP_K) -> list[str]:
        """Return up to top_k names from fused dense and BM25 rankings.

        A lexical-only result survives embedding outages. Returns [] when the
        stored index cannot match at all, so callers expose the full catalog.
        """
        if self._cache is None:
            rows = await asyncio.to_thread(self._load_all_sync)
            parsed: list[tuple[str, str, list[float]]] = []
            for name, profile, emb_json in rows:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    parsed.append((name, profile, json.loads(emb_json)))
            self._cache = parsed

        profiles = {name: profile for name, profile, _ in self._cache}
        lexical = bm25_rank(query, profiles)
        dense: list[str] = []
        try:
            q_embs = await self._embed_fn([query])
            if q_embs:
                qvec = q_embs[0]
                scored = [
                    (name, cosine_similarity(qvec, embedding))
                    for name, _, embedding in self._cache
                ]
                scored.sort(key=lambda item: (-item[1], item[0]))
                dense = [name for name, _ in scored]
        except Exception:
            logger.debug("ToolIndex: dense query failed; using lexical ranking", exc_info=True)

        fused = reciprocal_rank_fusion(dense, lexical, exact_query=query)
        return fused[:top_k]

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

    def _upsert_many_sync(self, rows: list[tuple[str, str, str]]) -> None:
        """Upsert a batch in one transaction."""
        from utils.time import utcnow

        now = utcnow().isoformat()
        with open_db_connection(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO tool_embeddings (name, description, embedding, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, "
                "embedding=excluded.embedding, "
                "updated_at=excluded.updated_at",
                [(name, desc, emb, now) for name, desc, emb in rows],
            )

    def _all_descriptions_sync(self) -> dict[str, str]:
        """Every indexed tool's stored description, for the batch freshness check."""
        with open_db_connection(self._db_path) as conn:
            return {r["name"]: r["description"] for r in conn.execute("SELECT name, description FROM tool_embeddings")}

    def _get_description_sync(self, name: str) -> str | None:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute("SELECT description FROM tool_embeddings WHERE name = ?", (name,)).fetchone()
        return row["description"] if row is not None else None

    def _delete_sync(self, name: str) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM tool_embeddings WHERE name = ?", (name,))

    def _load_all_sync(self) -> list[tuple[str, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT name, description, embedding FROM tool_embeddings"
            ).fetchall()
        return [(r["name"], r["description"], r["embedding"]) for r in rows]
