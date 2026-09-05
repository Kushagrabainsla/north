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
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.dependencies import EmbedFn, SupersedeFn
from utils.db import open_db_connection
from utils.ids import generate_id
from utils.math import cosine_similarity
from utils.vector_space import ensure_vector_space_reembed

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
    observed_at     DATETIME,
    superseded_by   TEXT
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
    "ALTER TABLE context_facts ADD COLUMN superseded_by TEXT",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_cat_updated ON context_facts (category, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_updated ON context_facts (updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_context_facts_cat_content ON context_facts (category, content)",
]

_MAX_FACTS_RETURNED: int = 15
_DEDUP_SIMILARITY_THRESHOLD: float = 0.85
# How a recalled fact is judged "close enough" to the query.
#
# This was a single absolute floor of 0.25, and that number silently broke recall
# when the embedding model changed underneath it. Absolute cosine values are a
# property of the *model*, not of relevance: on the static model that shipped in
# 1.13.0 an identity question like "who am I" scored 0.096 against the fact that
# answered it correctly - ranked first, and thrown away by the floor. The store
# then returned nothing, and the agent truthfully reported it knew nothing about
# the user while holding 220 facts about them.
#
# So the test is mostly *relative*: keep what is close to the best match for this
# query, whatever that scale happens to be. The absolute floor stays only as a
# sanity check for a query no fact is about, at a value low enough that it cannot
# be what decides an ordinary recall.
_RECALL_MIN_SIMILARITY: float = 0.30
# A fact more than this far below the query's best match is a worse answer to a
# question already answered, not a second answer. Measured against 220 real
# facts: 0.05 keeps recall identical to a wide margin (the right fact is still
# retrieved for 93% of questions) while cutting a typical personal question from
# 15 injected facts to 5, because a question with a real answer has a *peaked*
# score distribution - a few facts stand out and the rest fall away.
#
# What this cannot do is decide whether to inject anything at all. On the same
# data an unrelated prompt ("deploy to production") scores 0.678 against its best
# fact while a real question ("what school do I go to") scores 0.534, so the two
# populations overlap completely and no threshold separates them. Whether a task
# wants personal context is a property of the task, not of a cosine score.
_RECALL_RELATIVE_MARGIN: float = 0.05
# The best few candidates always reach the model, margin or not. On held-out
# questions the margin alone dropped a fact ranked *third* because it sat 0.053
# below the top hit - and a fact the model never sees is one it cannot use,
# whereas a wrong fact among three is one it can ignore. Widening the margin
# instead would buy the same recall at twice the injected facts.
_RECALL_ALWAYS_KEEP: int = 3
# Retention cap: the store holds at most this many facts (oldest evicted on
# insert), which also bounds every cosine scan and the in-memory cache.
_MAX_FACTS_STORED: int = 5_000
# Dedup-on-insert only compares against the most recent rows per category  -
# an O(all rows) scan per insert does not scale and recent facts are the
# plausible duplicates anyway.
_DEDUP_SCAN_LIMIT: int = 500
# A fact is only *retrieved* while its status is this. Superseded rows stay in
# the table - they are the record of what was once true, and deleting them would
# make the change invisible - but they never reach a prompt again.
_STATUS_ACTIVE: str = "active"
_STATUS_SUPERSEDED: str = "superseded"
# The band where a new fact is close enough to an old one to be *about the same
# thing*, but not close enough to be the same claim. Below _DEDUP_SIMILARITY_
# THRESHOLD the store already merges; this is the range underneath it, where a
# contradiction can hide. Measured on real facts: "the user currently interns at
# LinkedIn" scores 0.81 against the fact recording that the internship ended and
# 0.68 against the fact dating its end - both would have been missed entirely by
# a check that only looked at near-identical text.
_SUPERSEDE_MIN_SIMILARITY: float = 0.60
# At most this many candidates are shown to the supersede check, so one insert
# cannot turn into an unbounded prompt. Not smaller: the fact most in need of
# retiring is often *not* the most similar one. Recording that an internship
# ended scores 0.84 against another fact about it ending, but only 0.70 against
# the stale "currently interns at ..." that actually had to go - which sat 8th.
# A cap of 5 silently missed every fact worth superseding.
_SUPERSEDE_MAX_CANDIDATES: int = 20


def _apply_recall_cutoff(scored: list[tuple[str, float]], max_results: int) -> list[str]:
    """Keep the facts worth injecting from *scored*, best first.

    Past the first _RECALL_ALWAYS_KEEP, a fact survives if it is within
    _RECALL_RELATIVE_MARGIN of the best match for this query and clears the
    absolute sanity floor. The relative test does the real work: judging
    relevance by absolute cosine alone ties recall to whatever numeric range the
    current embedding model happens to produce, which is exactly how a model swap
    silently emptied this store.
    """
    if not scored:
        return []
    best = scored[0][1]
    cutoff = max(_RECALL_MIN_SIMILARITY, best - _RECALL_RELATIVE_MARGIN)
    return [
        content
        for rank, (content, score) in enumerate(scored[:max_results])
        if rank < _RECALL_ALWAYS_KEEP or score >= cutoff
    ]


class FactStore:
    """Per-fact storage with per-entry embeddings for semantic context injection.

    Extractions write atomic facts here (one sentence each). Context injection
    queries by cosine similarity instead of loading entire markdown documents.
    Falls back to recency ordering when embedding is unavailable.
    """

    def __init__(
        self,
        db_path: Path,
        embed_fn: EmbedFn,
        embedding_model: str = "",
        supersede_fn: SupersedeFn | None = None,
    ) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        # Optional: without it the store simply never supersedes, which is the
        # behaviour this replaces rather than a new failure mode.
        self._supersede_fn = supersede_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.executescript(_SCHEMA)
            self._run_migrations(conn)
        # A fact is not derived data - the document it was extracted from may be
        # long gone - so a model change clears its vector and keeps its content,
        # and `backfill_embeddings` re-embeds it. Until that runs the store still
        # answers, from recency rather than similarity. The return value is not
        # kept: the backfill has to run anyway for facts written while the
        # embedder was down, and it is a no-op when there is nothing to embed.
        ensure_vector_space_reembed(self._db_path, embedding_model, (("context_facts", "embedding"),))
        # (id, content, embedding_vector, category, subject, status) - rebuilt lazily, invalidated on insert.
        self._cache: list[tuple[str, str, list[float], str, str, str]] | None = None
        # Serializes cache rebuilds: concurrent searches after an invalidation
        # must not interleave loads and clobber each other's cache.
        self._cache_lock = asyncio.Lock()

    async def _supersede_contradicted(self, content: str, category: str, new_emb: list[float]) -> int:
        """Retire active facts that *content* makes untrue. Returns how many.

        Only runs against facts in the band below the dedup threshold: close
        enough to be about the same subject, far enough not to be the same claim.
        Above that band the store already merges the rows, and below it the two
        facts are about different things and cannot contradict.

        A failure here is not allowed to fail the write. The new fact is the more
        current of the two, so storing it and leaving the old one active is a
        strictly better outcome than losing it.
        """
        if self._supersede_fn is None or not new_emb:
            return 0
        try:
            candidates = await asyncio.to_thread(
                self._candidates_in_band_sync, category, new_emb,
                _SUPERSEDE_MIN_SIMILARITY, _DEDUP_SIMILARITY_THRESHOLD,
            )
            if not candidates:
                return 0
            stale = await self._supersede_fn(content, [c[1] for c in candidates])
            retired = [candidates[i][0] for i in stale if 0 <= i < len(candidates)]
            if not retired:
                return 0
            await asyncio.to_thread(self._mark_superseded_sync, retired, None)
            self.invalidate_cache()
            for fact_id, text in candidates:
                if fact_id in retired:
                    logger.info("FactStore: superseded %r - contradicted by %r", text[:80], content[:80])
            return len(retired)
        except Exception:
            logger.warning("FactStore: supersede check failed - the older fact stays active", exc_info=True)
            return 0

    def _candidates_in_band_sync(
        self, category: str, emb: list[float], low: float, high: float
    ) -> list[tuple[str, str]]:
        """Active facts in *category* whose similarity to *emb* falls in [low, high)."""
        emb_json = json.dumps(emb)
        sql = """
        SELECT id, content FROM (
            SELECT id, content, (1.0 - vec_distance_cosine(embedding, ?)) AS sim
            FROM context_facts
            WHERE category = ? AND status = ?
              AND embedding IS NOT NULL AND embedding != '' AND embedding != '[]'
            ORDER BY updated_at DESC LIMIT ?
        )
        WHERE sim >= ? AND sim < ?
        ORDER BY sim DESC LIMIT ?
        """
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    sql, (emb_json, category, _STATUS_ACTIVE, _DEDUP_SCAN_LIMIT,
                          low, high, _SUPERSEDE_MAX_CANDIDATES),
                ).fetchall()
            return [(r["id"], r["content"]) for r in rows]
        except Exception:
            # No sqlite-vec: fall back to scoring in Python over the recent window.
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT id, content, embedding FROM context_facts "
                    "WHERE category = ? AND status = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, _STATUS_ACTIVE, _DEDUP_SCAN_LIMIT),
                ).fetchall()
            scored: list[tuple[float, str, str]] = []
            for row in rows:
                if not row["embedding"]:
                    continue
                try:
                    existing = json.loads(row["embedding"])
                except (json.JSONDecodeError, ValueError):
                    continue
                if not existing or len(existing) != len(emb):
                    continue
                sim = cosine_similarity(emb, existing)
                if low <= sim < high:
                    scored.append((sim, row["id"], row["content"]))
            scored.sort(reverse=True)
            return [(i, c) for _, i, c in scored[:_SUPERSEDE_MAX_CANDIDATES]]

    def _mark_superseded_sync(self, fact_ids: list[str], replaced_by: str | None) -> int:
        if not fact_ids:
            return 0
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            placeholders = ",".join("?" for _ in fact_ids)
            return conn.execute(
                f"UPDATE context_facts SET status = ?, superseded_by = ?, updated_at = ? "  # noqa: S608
                f"WHERE id IN ({placeholders})",
                (_STATUS_SUPERSEDED, replaced_by, now, *fact_ids),
            ).rowcount

    async def supersede_fact(self, fact_id: str, replaced_by: str | None = None) -> bool:
        """Retire one fact by id. The row is kept; it just stops being recalled."""
        changed = await asyncio.to_thread(self._mark_superseded_sync, [fact_id], replaced_by)
        if changed:
            self.invalidate_cache()
        return changed > 0

    async def backfill_embeddings(self, batch_size: int = 128) -> int:
        """Embed every fact that has no vector. Returns how many were embedded.

        Runs after a model change and on any fact written while the embedder was
        down. Batched, because one call per fact would pay the model's per-call
        overhead hundreds of times over.
        """
        pending = await asyncio.to_thread(self._unembedded_sync)
        if not pending:
            return 0
        done = 0
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            try:
                vectors = await self._embed_fn([content for _, content in batch])
            except Exception:
                logger.warning("FactStore: backfill batch failed - leaving it for the next run", exc_info=True)
                break
            if len(vectors) != len(batch):
                logger.warning("FactStore: backfill got %d vectors for %d facts - stopping", len(vectors), len(batch))
                break
            await asyncio.to_thread(
                self._store_embeddings_sync,
                [(fact_id, json.dumps(vec)) for (fact_id, _), vec in zip(batch, vectors, strict=True)],
            )
            done += len(batch)
        if done:
            self.invalidate_cache()
            logger.info("FactStore: re-embedded %d fact(s) into the current embedding space", done)
        return done

    def _unembedded_sync(self) -> list[tuple[str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, content FROM context_facts "
                "WHERE embedding IS NULL OR embedding = '' OR embedding = '[]'"
            ).fetchall()
        return [(r["id"], r["content"]) for r in rows]

    def _store_embeddings_sync(self, pairs: list[tuple[str, str]]) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.executemany("UPDATE context_facts SET embedding = ? WHERE id = ?", [(e, i) for i, e in pairs])

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
        # Runs after the write, not before: learning that "the internship ended"
        # is only worth acting on once that fact is safely stored, and a new fact
        # is the more current of any pair it contradicts.
        if inserted:
            await self._supersede_contradicted(content, category, new_emb)
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

        # Fallback path: in-memory cache with Python cosine calculation.
        # The length guard is not paranoia: comparing vectors of different sizes
        # raises inside numpy, and one stray row would take down the whole recall
        # rather than being skipped as the unusable row it is.
        scored = [
            (content, cosine_similarity(qvec, emb))
            for _, content, emb, category, _, _ in cache
            if emb
            and len(emb) == len(qvec)
            and (allowed_categories is None or category in allowed_categories)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return _apply_recall_cutoff(scored, max_results)

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
                "WHERE embedding IS NOT NULL AND embedding != '' AND embedding != '[]' "
                "AND status = ?"
            )
            params: list[Any] = [qvec_json, _STATUS_ACTIVE]
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
            # The relative half of the cutoff needs the best score for this query,
            # which SQL only knows once the rows are ordered - so SQL applies the
            # absolute floor and the margin is applied to the result below.

            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
            return _apply_recall_cutoff([(r["content"], r["similarity"]) for r in rows], max_results)
        except Exception:
            return None

    async def count(self, category: str | None = None) -> int:
        """Total facts, or only those in *category* when given."""
        return await asyncio.to_thread(self._count_sync, category)

    async def count_in_categories(self, categories: Iterable[str]) -> int:
        """How many facts sit in any of *categories*.

        Bootstrap files its facts under a topic rather than one flat category, so
        "has bootstrap already run?" is a question about a *set* of categories -
        and it must stay distinguishable from facts learned in conversation,
        which are filed under their context document instead.
        """
        names = [c for c in categories if c]
        if not names:
            return 0
        return await asyncio.to_thread(self._count_in_categories_sync, names)

    def _count_in_categories_sync(self, names: list[str]) -> int:
        placeholders = ",".join("?" for _ in names)
        with open_db_connection(self._db_path) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM context_facts WHERE category IN ({placeholders})",  # noqa: S608
                names,
            ).fetchone()[0]

    async def all_facts(self, category: str | None = None) -> list[dict]:
        """Return all facts for web UI display, optionally filtered by category."""
        return await asyncio.to_thread(self._all_facts_sync, category)

    async def update_fact(self, fact_id: str, content: str, category: str | None = None) -> bool:
        content = content.strip()
        if not content or _contains_secret(content):
            return False
        try:
            vectors = await self._embed_fn([content])
            embedding = json.dumps(vectors[0] if vectors else [])
        except Exception:
            embedding = json.dumps([])
        return await asyncio.to_thread(self._update_fact_sync, fact_id, content, category, embedding)

    def _update_fact_sync(self, fact_id: str, content: str, category: str | None, embedding: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with open_db_connection(self._db_path) as conn:
            if category is None:
                result = conn.execute(
                    "UPDATE context_facts SET content=?, embedding=?, updated_at=? WHERE id=?",
                    (content, embedding, now, fact_id),
                )
            else:
                result = conn.execute(
                    "UPDATE context_facts SET content=?, category=?, embedding=?, updated_at=? WHERE id=?",
                    (content, category, embedding, now, fact_id),
                )
        if result.rowcount:
            self.invalidate_cache()
        return result.rowcount > 0

    async def delete_fact(self, fact_id: str) -> bool:
        deleted = await asyncio.to_thread(self._delete_fact_sync, fact_id)
        if deleted:
            self.invalidate_cache()
        return deleted

    def _delete_fact_sync(self, fact_id: str) -> bool:
        with open_db_connection(self._db_path) as conn:
            return conn.execute("DELETE FROM context_facts WHERE id=?", (fact_id,)).rowcount > 0

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
                "SELECT id, content, embedding, category, subject, status FROM context_facts "
                "WHERE status = ?",
                (_STATUS_ACTIVE,),
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
                    f"SELECT content FROM context_facts WHERE status = ? AND category IN ({placeholders}) "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (_STATUS_ACTIVE, *allowed_categories, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM context_facts WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (_STATUS_ACTIVE, limit),
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
                    "SELECT id, content, category, subject, confidence, status, updated_at FROM context_facts "
                    "WHERE category = ? ORDER BY updated_at DESC",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, category, subject, confidence, status, updated_at "
                    "FROM context_facts ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]
