"""Atomic fact store for personal context, backed by SQLite + per-entry embeddings.

Each extracted fact is stored as one row, embedded individually, and retrieved
by cosine similarity at agent load time. This replaces the flat-markdown "load
everything" approach with targeted semantic retrieval (~15 facts vs. full docs).

The markdown files in FileContextStore remain as a human-readable mirror - facts
are written to both, so the web UI and existing backup/trim logic stay intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.dependencies import EmbedFn
from utils.db import open_db_connection
from utils.ids import generate_id
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

# Secret detection patterns
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:^|[\s\W])
    (?:
        (?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token)
        |(?:password|passwd|pwd)
        |(?:private[_-]?key|ssh[_-]?key)
        |(?:aws[_-]?access[_-]?key|aws[_-]?secret[_-]?key)
        |(?:github[_-]?token|gh[_-]?token|ghp_)
        |(?:slack[_-]?token|xox[baprs]-)
        |(?:stripe[_-]?key|sk_live_|pk_live_)
        |(?:jwt[_-]?token|eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*)
        |(?:credit[_-]?card|cc[_-]?num)
        |(?:seed[_-]?phrase|mnemonic)
    )
    [\s:=]+
    [A-Za-z0-9_\-+/=]{8,}
    """,
)

_CC_RE = re.compile(
    r"\b(?:\d[ -]*?){13,19}\b"
)

def _contains_secret(text: str) -> bool:
    """Check if text contains secrets, API keys, passwords, credit cards, etc."""
    return bool(_SECRET_RE.search(text) or _CC_RE.search(text))

def _normalize_for_dedup(text: str) -> str:
    """Normalize text for deduplication comparison.
    
    Lowercases, removes punctuation, normalizes whitespace, removes common filler words.
    """
    # Lowercase
    text = text.lower()
    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove common filler words that don't affect meaning
    filler_words = {'the', 'a', 'an', 'is', 'was', 'were', 'am', 'are', 'be', 'been', 'being',
                    'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
                    'my', 'your', 'his', 'her', 'their', 'our', 'its', 'this', 'that', 'these', 'those',
                    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'as', 'or', 'and', 'but'}
    words = [w for w in text.split() if w not in filler_words]
    return ' '.join(words)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_facts (
    id              TEXT     NOT NULL PRIMARY KEY,
    content         TEXT     NOT NULL,
    category        TEXT     NOT NULL DEFAULT 'user',
    embedding       TEXT,
    updated_at      DATETIME NOT NULL,
    subject         TEXT     NOT NULL DEFAULT 'user',
    confidence      REAL     NOT NULL DEFAULT 0.8,
    status          TEXT     NOT NULL DEFAULT 'active',
    source_path     TEXT,
    source_hash     TEXT,
    source_mtime    REAL,
    evidence        TEXT,
    observed_at     DATETIME
);
CREATE INDEX IF NOT EXISTS idx_context_facts_cat_updated ON context_facts (category, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_facts_updated ON context_facts (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_facts_cat_content ON context_facts (category, content);
"""

# Migration: add new columns and indexes if they don't exist (for existing databases)
_MIGRATION_STATEMENTS = [
    "ALTER TABLE context_facts ADD COLUMN subject TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE context_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 0.8",
    "ALTER TABLE context_facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE context_facts ADD COLUMN source_path TEXT",
    "ALTER TABLE context_facts ADD COLUMN source_hash TEXT",
    "ALTER TABLE context_facts ADD COLUMN source_mtime REAL",
    "ALTER TABLE context_facts ADD COLUMN evidence TEXT",
    "ALTER TABLE context_facts ADD COLUMN observed_at DATETIME",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_cat_updated ON context_facts (category, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_updated ON context_facts (updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_cat_content ON context_facts (category, content)",
]

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
            conn.executescript(_SCHEMA)
            self._run_migrations(conn)
        # (id, content, embedding_vector, category, subject, status) - rebuilt lazily, invalidated on insert.
        self._cache: list[tuple[str, str, list[float], str, str, str]] | None = None
        # Serializes cache rebuilds: concurrent searches after an invalidation
        # must not interleave loads and clobber each other's cache.
        self._cache_lock = asyncio.Lock()

    def _run_migrations(self, conn) -> None:
        """Run schema migrations for existing databases."""
        for stmt in _MIGRATION_STATEMENTS:
            with contextlib.suppress(Exception):
                # Column may already exist; ignore
                conn.execute(stmt)

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
        return await self.add_fact_with_provenance(content, category)

    async def add_fact_with_provenance(
        self,
        content: str,
        category: str = "user",
        subject: str = "user",
        confidence: float = 0.8,
        status: str = "active",
        source_path: str | None = None,
        source_hash: str | None = None,
        source_mtime: float | None = None,
        evidence: str | None = None,
    ) -> bool:
        """Embed and persist one fact with full provenance. Returns True when a new row was inserted.

        This is the extended version used by bootstrap and other provenance-aware writers.
        """
        content = content.strip()
        if not content:
            return False
        
        # Filter secrets before persistence - defense in depth
        if _contains_secret(content):
            logger.warning("FactStore: rejected fact containing secret/credential")
            return False
        
        # Exact-match dedup runs BEFORE any embedding call: identical content
        # is a duplicate by definition, and skipping the embed for it saves
        # rate-limit budget as well as rows.
        if await asyncio.to_thread(self._find_exact_sync, category, content):
            await asyncio.to_thread(self._touch_sync, category, content)
            return False
        
        # Normalized text dedup (catches paraphrases like
        # "User studies CS at SJSU" vs "User studies computer science at San Jose State University")
        matched_id = await asyncio.to_thread(self._find_normalized_sync, category, content)
        if matched_id:
            await asyncio.to_thread(self._touch_by_id_sync, matched_id)
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

        inserted, fact_id = await asyncio.to_thread(
            self._insert_or_replace_sync,
            content, category, emb_json, replace_id,
            subject, confidence, status,
            source_path, source_hash, source_mtime, evidence
        )
        if self._cache is not None:
            if not inserted and replace_id:
                self._cache = [
                    (item[0], content, new_emb, category, subject, status) if item[0] == replace_id else item
                    for item in self._cache
                ]
            else:
                self._cache.append((fact_id, content, new_emb, category, subject, status))
                if len(self._cache) > _MAX_FACTS_STORED:
                    self._cache = self._cache[-_MAX_FACTS_STORED:]
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
        if not q_embs or not q_embs[0]:
            return await asyncio.to_thread(self._recent_facts_sync, max_results, allowed_categories)
        qvec = q_embs[0]

        cache = await self._get_cache()
        if not cache:
            # No stored fact has a usable embedding (they were written while
            # the embedder was down): cosine ranking would match nothing.
            # Return the most recent facts so recall still answers.
            return await asyncio.to_thread(self._recent_facts_sync, max_results, allowed_categories)

        # Fast path: native sqlite-vec vector search in SQL
        results = await asyncio.to_thread(self._search_vector_sync, qvec, max_results, allowed_categories)
        if results is not None:
            return results

        # Fallback path: in-memory cache with Python cosine calculation
        scored = [
            (content, cosine_similarity(qvec, emb))
            for _, content, emb, category, _, _ in cache
            if emb and (allowed_categories is None or category in allowed_categories)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [content for content, score in scored[:max_results] if score >= _RECALL_MIN_SIMILARITY]

    def _search_vector_sync(
        self,
        qvec: list[float],
        max_results: int,
        allowed_categories: frozenset[str] | None,
    ) -> list[str] | None:
        """Native sqlite-vec query in SQLite; returns None if sqlite-vec is unavailable."""
        try:
            qvec_json = json.dumps(qvec)
            sql = (
                "SELECT content, (1.0 - vec_distance_cosine(embedding, ?)) AS similarity "
                "FROM context_facts "
                "WHERE embedding IS NOT NULL AND embedding != '' AND embedding != '[]'"
            )
            params: list[Any] = [qvec_json]
            if allowed_categories is not None:
                if not allowed_categories:
                    return []
                placeholders = ",".join("?" for _ in allowed_categories)
                sql += f" AND category IN ({placeholders})"
                params.extend(allowed_categories)
            sql += " AND (1.0 - vec_distance_cosine(embedding, ?)) >= ? "
            params.append(qvec_json)
            params.append(_RECALL_MIN_SIMILARITY)
            sql += " ORDER BY similarity DESC LIMIT ?"
            params.append(max_results)

            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
            return [r["content"] for r in rows]
        except Exception:
            return None

    async def count(self, category: str | None = None) -> int:
        """Total facts, or only those in *category* when given."""
        return await asyncio.to_thread(self._count_sync, category)

    async def all_facts(self, category: str | None = None) -> list[dict]:
        """Return all facts for web UI display, optionally filtered by category."""
        return await asyncio.to_thread(self._all_facts_sync, category)

    def invalidate_cache(self) -> None:
        self._cache = None

    async def _get_cache(self) -> list[tuple[str, str, list[float], str, str, str]]:
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
                parsed: list[tuple[str, str, list[float], str, str, str]] = []
                for row_id, content, emb_json, category, subject, status in rows:
                    if emb_json:
                        try:
                            emb = json.loads(emb_json)
                            if emb:
                                parsed.append((row_id, content, emb, category, subject, status))
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

    def _find_normalized_sync(self, category: str, content: str) -> str | None:
        """Find existing fact with normalized text match in *category*.
        
        Returns the id of the matching fact if found, else None.
        """
        normalized = _normalize_for_dedup(content)
        if not normalized:
            return None
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, content FROM context_facts WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, _DEDUP_SCAN_LIMIT),
            ).fetchall()
        for row in rows:
            existing_normalized = _normalize_for_dedup(row["content"])
            if existing_normalized == normalized:
                return row["id"]
        return None

    def _touch_sync(self, category: str, content: str) -> None:
        """Refresh recency of an existing fact (exact-match dedup path)."""
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE context_facts SET updated_at = ? WHERE category = ? AND content = ?",
                (datetime.now(UTC).isoformat(), category, content),
            )

    def _touch_by_id_sync(self, fact_id: str) -> None:
        """Refresh recency of an existing fact by ID (normalized dedup path)."""
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE context_facts SET updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), fact_id),
            )

    def _find_similar_sync(self, category: str, emb: list[float], threshold: float) -> str | None:
        """Return the id of a recent fact in *category* with similarity >= threshold, or None.

        Bounded to the most recent _DEDUP_SCAN_LIMIT rows so insert cost does
        not grow with total store size.
        """
        emb_json = json.dumps(emb)
        try:
            sql = """
            SELECT id FROM (
                SELECT id, (1.0 - vec_distance_cosine(embedding, ?)) as sim
                FROM context_facts
                WHERE category = ? AND embedding IS NOT NULL AND embedding != '' AND embedding != '[]'
                ORDER BY updated_at DESC LIMIT ?
            )
            WHERE sim >= ?
            ORDER BY sim DESC LIMIT 1
            """
            with open_db_connection(self._db_path) as conn:
                row = conn.execute(sql, (emb_json, category, _DEDUP_SCAN_LIMIT, threshold)).fetchone()
                if row is not None:
                    return row["id"]
                return None
        except Exception:
            pass

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

    def _insert_or_replace_sync(self, content: str, category: str, emb_json: str, replace_id: str | None,
                             subject: str = "user", confidence: float = 0.8, status: str = "active",
                             source_path: str | None = None, source_hash: str | None = None,
                             source_mtime: float | None = None, evidence: str | None = None) -> tuple[bool, str]:
        """Insert *content*; returns (is_new_row, fact_id).

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
                return False, replace_id
            fact_id = generate_id()
            conn.execute(
                """INSERT INTO context_facts
                (id, content, category, embedding, updated_at, subject, confidence, status,
                 source_path, source_hash, source_mtime, evidence, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fact_id, content, category, emb_json, now, subject, confidence, status,
                 source_path, source_hash, source_mtime, evidence, now),
            )
            # Retention: evict the oldest facts beyond the cap so the store
            # (and every scan over it) stays bounded.
            conn.execute(
                "DELETE FROM context_facts WHERE id NOT IN "
                "(SELECT id FROM context_facts ORDER BY updated_at DESC LIMIT ?)",
                (_MAX_FACTS_STORED,),
            )
            return True, fact_id

    def _load_all_sync(self) -> list[tuple[str, str, str, str, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, content, embedding, category, subject, status FROM context_facts"
            ).fetchall()
        return [
            (
                r["id"],
                r["content"],
                r["embedding"] or "",
                r["category"],
                r["subject"] or "user",
                r["status"] or "active",
            )
            for r in rows
        ]

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
