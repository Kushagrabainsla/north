"""Episodic memory: per-task summaries with embedding-based retrieval.

After a task reaches a terminal state (success, failed, or cancelled) an
episode is recorded here, one row per task. Before each agent run the memory
gateway queries this store for the top-k most semantically similar past
episodes and injects them as context, so north avoids repeating past mistakes
and re-asking settled questions. Failed and cancelled episodes are labelled on
retrieval so they read as cautionary, not as a template to copy.

Retrieval falls back to keyword search when the embed function is unavailable
(e.g. tests, offline mode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection

from config.dependencies import EmbedFn
from utils.db import open_db_connection
from utils.ids import generate_id
from utils.text import STOPWORDS
from utils.vector_space import ensure_vector_space_reembed

logger = logging.getLogger(__name__)

# Episodes are pruned on write only by age: rows older than the retention window
# are deleted. There is no row-count cap for now (uncapped), so a full year of
# task history is retained and searched. The cosine scan is over all kept rows.
_RETENTION_DAYS = 365  # episodes older than this (1-year window) are pruned on write

# "partial" is a task that finished but did not fully do what was asked. It is
# kept for recall - knowing an approach half-worked is useful - but it is not a
# success, so `list_successful` will not offer it to the skill distiller as an
# exemplar to learn a procedure from.
_VALID_OUTCOMES = frozenset({"success", "partial", "failed", "cancelled"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id         TEXT    PRIMARY KEY,
    task_id    TEXT,
    domain     TEXT    NOT NULL,
    outcome    TEXT    NOT NULL DEFAULT 'success',
    summary    TEXT    NOT NULL,
    embedding  TEXT,
    timestamp  TEXT    NOT NULL,
    updated_at TEXT
)
"""

_SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS idx_episodes_domain ON episodes (domain)"
# Non-unique: supports the per-task upsert (delete-then-insert) and the
# consolidator's per-task lookups without failing on any legacy duplicate rows.
_SCHEMA_TASK_INDEX = "CREATE INDEX IF NOT EXISTS idx_episodes_task ON episodes (task_id)"

# Columns added after v1; _migrate() backfills them on existing databases.
_ADDED_COLUMNS: dict[str, str] = {
    "outcome": "TEXT NOT NULL DEFAULT 'success'",
    "updated_at": "TEXT",
}


def _label(summary: str, outcome: str) -> str:
    """Prefix non-success summaries so retrieval marks them as cautionary.

    A failed past attempt injected as plain context invites the model to repeat
    it; the label makes it an avoid-this signal instead.
    """
    if outcome == "failed":
        return f"[FAILED] {summary}"
    if outcome == "cancelled":
        return f"[CANCELLED] {summary}"
    if outcome == "partial":
        return f"[PARTIAL] {summary}"
    return summary


def _keyword_score(text: str, query_words: frozenset[str]) -> int:
    words = frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS)
    return len(query_words & words)


class EpisodicStore:
    """Stores and retrieves per-task episodic summaries."""

    def __init__(self, db_path: Path, embed_fn: EmbedFn | None = None, embedding_model: str = "") -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_SCHEMA_INDEX)
            conn.execute(_SCHEMA_TASK_INDEX)
            self._migrate(conn)
        # An episode is a record of something that happened; it cannot be rebuilt
        # from anything on disk, so a model change clears its vector and keeps the
        # summary. Search already falls back to keyword overlap without one.
        ensure_vector_space_reembed(self._db_path, embedding_model, (("episodes", "embedding"),))

    @staticmethod
    def _migrate(conn: Connection) -> None:
        """Add columns introduced after v1 to a pre-existing episodes table.

        CREATE TABLE IF NOT EXISTS does not alter an existing table, so older
        databases miss `outcome`/`updated_at`; add any that are absent.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
        for column, decl in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE episodes ADD COLUMN {column} {decl}")

    async def record(self, task_id: str, domain: str, summary: str, outcome: str = "success") -> None:
        """Upsert one episode per task (success, failed, or cancelled) and prune old rows.

        Re-recording the same `task_id` replaces the prior row, so a task that
        was retried or moved from running to a terminal state keeps a single,
        current episode.
        """
        if outcome not in _VALID_OUTCOMES:
            outcome = "success"
        embedding: list[float] | None = None
        if self._embed_fn is not None:
            try:
                vecs = await self._embed_fn([summary])
                embedding = vecs[0] if vecs else None
            except Exception:
                logger.debug("EpisodicStore: embed failed for task %s", task_id)
        now = datetime.now(UTC).isoformat()
        emb_json = json.dumps(embedding) if embedding is not None else None
        await asyncio.to_thread(
            self._upsert_and_prune_sync,
            generate_id(),
            task_id,
            domain,
            outcome,
            summary,
            emb_json,
            now,
        )

    async def search(
        self,
        query: str,
        max_results: int = 3,
        allowed_domains: frozenset[str] | None = None,
    ) -> list[str]:
        """Return the most relevant past episode summaries for *query*.

        When *allowed_domains* is given, only episodes from those domains are
        considered, so a caller never receives another domain's task history.
        Tries embedding cosine similarity first; falls back to keyword overlap
        scoring so retrieval always works.
        """
        if self._embed_fn is not None:
            try:
                vecs = await self._embed_fn([query])
                if vecs and vecs[0]:
                    results = await asyncio.to_thread(self._search_vector_sync, vecs[0], max_results, allowed_domains)
                    if results:
                        return results
            except Exception:
                pass

        rows = await asyncio.to_thread(self._load_all_sync, allowed_domains)
        if not rows:
            return []

        # Keyword fallback
        query_words = frozenset(w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS)
        kw_scored = sorted(
            rows,
            key=lambda r: _keyword_score(r[1], query_words),
            reverse=True,
        )
        return [_label(summary, outcome) for _, summary, _, outcome, _ in kw_scored[:max_results] if summary]

    def _search_vector_sync(
        self,
        qvec: list[float],
        max_results: int = 3,
        allowed_domains: frozenset[str] | None = None,
    ) -> list[str] | None:
        """Native sqlite-vec vector search in SQLite; returns None if sqlite-vec is unavailable."""
        try:
            qvec_json = json.dumps(qvec)
            sql = (
                "SELECT summary, outcome, (1.0 - vec_distance_cosine(embedding, ?)) AS similarity "
                "FROM episodes WHERE embedding IS NOT NULL AND embedding != '' AND embedding != '[]'"
            )
            params: list[object] = [qvec_json]
            if allowed_domains is not None:
                if not allowed_domains:
                    return []
                placeholders = ",".join("?" for _ in allowed_domains)
                sql += f" AND domain IN ({placeholders})"
                params.extend(allowed_domains)
            sql += " AND (1.0 - vec_distance_cosine(embedding, ?)) > 0.3 "
            params.append(qvec_json)
            sql += " ORDER BY similarity DESC LIMIT ?"
            params.append(max_results)

            with open_db_connection(self._db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
            return [_label(r["summary"], r["outcome"] or "success") for r in rows if r["summary"]]
        except Exception:
            return None

    # ------------------------------------------------------------------ #

    async def list_successful(self, domains: frozenset[str]) -> list[tuple[str, str, list[float] | None]]:
        """Return ``(task_id, summary, embedding)`` for successful episodes in *domains*.

        Used by the skill distiller to find recurring successful patterns worth
        turning into a reusable skill. Embeddings are parsed back from JSON;
        episodes stored without one (embed unavailable at record time) yield None.
        """
        rows = await asyncio.to_thread(self._load_successful_sync, domains)
        result: list[tuple[str, str, list[float] | None]] = []
        for task_id, summary, emb_json in rows:
            embedding: list[float] | None = None
            if emb_json:
                try:
                    embedding = json.loads(emb_json)
                except json.JSONDecodeError:
                    embedding = None
            result.append((task_id, summary, embedding))
        return result

    def _load_successful_sync(self, domains: frozenset[str]) -> list[tuple[str, str, str | None]]:
        if not domains:
            return []
        placeholders = ",".join("?" for _ in domains)
        sql = (
            f"SELECT task_id, summary, embedding FROM episodes "
            f"WHERE outcome = 'success' AND task_id IS NOT NULL AND domain IN ({placeholders})"
        )
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(sql, tuple(domains)).fetchall()
        return [(r["task_id"], r["summary"], r["embedding"]) for r in rows]

    def _upsert_and_prune_sync(
        self,
        ep_id: str,
        task_id: str,
        domain: str,
        outcome: str,
        summary: str,
        emb_json: str | None,
        now: str,
    ) -> None:
        """Replace any existing episode for this task, insert the new one, prune old rows.

        One episode per task_id: an earlier row for the same task (e.g. a prior
        attempt) is deleted first so retrying or moving to a terminal state does
        not accumulate duplicates. Pruning is by age only; the row count is
        uncapped for now.
        """
        with open_db_connection(self._db_path) as conn:
            if task_id:
                conn.execute("DELETE FROM episodes WHERE task_id = ?", (task_id,))
            conn.execute(
                "INSERT INTO episodes (id, task_id, domain, outcome, summary, embedding, timestamp, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ep_id, task_id, domain, outcome, summary, emb_json, now, now),
            )
            cutoff = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM episodes WHERE timestamp < ?", (cutoff,))

    def _load_all_sync(
        self, allowed_domains: frozenset[str] | None = None
    ) -> list[tuple[str, str, str | None, str, str]]:
        sql = "SELECT id, summary, embedding, outcome, domain FROM episodes"
        params: tuple[str, ...] = ()
        if allowed_domains is not None:
            if not allowed_domains:
                return []
            # Only "?" placeholders are interpolated here; the domain values are
            # bound as parameters below, so this is not a SQL-injection vector.
            placeholders = ",".join("?" for _ in allowed_domains)
            sql += f" WHERE domain IN ({placeholders})"
            params = tuple(allowed_domains)
        sql += " ORDER BY timestamp DESC"
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(r["id"], r["summary"], r["embedding"], r["outcome"] or "success", r["domain"]) for r in rows]
