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
from utils.math import cosine_similarity
from utils.text import STOPWORDS

logger = logging.getLogger(__name__)

# Episodes are pruned on write only by age: rows older than the retention window
# are deleted. There is no row-count cap for now (uncapped), so a full year of
# task history is retained and searched. The cosine scan is over all kept rows.
_RETENTION_DAYS = 365  # episodes older than this (1-year window) are pruned on write

_VALID_OUTCOMES = frozenset({"success", "failed", "cancelled"})

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
    return summary


def _keyword_score(text: str, query_words: frozenset[str]) -> int:
    words = frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS)
    return len(query_words & words)


class EpisodicStore:
    """Stores and retrieves per-task episodic summaries."""

    def __init__(self, db_path: Path, embed_fn: EmbedFn | None = None) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_SCHEMA_INDEX)
            conn.execute(_SCHEMA_TASK_INDEX)
            self._migrate(conn)

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
        rows = await asyncio.to_thread(self._load_all_sync)
        if allowed_domains is not None:
            rows = [r for r in rows if r[4] in allowed_domains]
        if not rows:
            return []

        if self._embed_fn is not None:
            try:
                vecs = await self._embed_fn([query])
                if vecs:
                    qvec = vecs[0]
                    scored: list[tuple[float, str]] = []
                    for _id, summary, emb_json, outcome, _domain in rows:
                        if emb_json is None:
                            continue
                        try:
                            emb = json.loads(emb_json)
                        except json.JSONDecodeError:
                            continue
                        scored.append((cosine_similarity(qvec, emb), _label(summary, outcome)))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    results = [s for _, s in scored[:max_results] if _ > 0.3]
                    if results:
                        return results
            except Exception:
                pass

        # Keyword fallback
        query_words = frozenset(w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS)
        kw_scored = sorted(
            rows,
            key=lambda r: _keyword_score(r[1], query_words),
            reverse=True,
        )
        return [_label(summary, outcome) for _, summary, _, outcome, _ in kw_scored[:max_results] if summary]

    # ------------------------------------------------------------------ #

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
            conn.commit()

    def _load_all_sync(self) -> list[tuple[str, str, str | None, str, str]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, summary, embedding, outcome, domain FROM episodes ORDER BY timestamp DESC"
            ).fetchall()
        return [(r["id"], r["summary"], r["embedding"], r["outcome"] or "success", r["domain"]) for r in rows]
