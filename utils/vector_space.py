"""One embedding space per vector store.

Similarity is only meaningful between vectors from the same model. Compare a
Gemini vector with a locally-produced one and cosine similarity still returns a
confident number - it is simply meaningless, and nothing in the result says so.

North's vector stores had no record of which model produced a row, so switching
or losing an embedding provider would have silently poisoned every search
against them. This module supplies the missing invariant, and it is deliberately
the *simplest* one that holds: **a store is either entirely in one embedding
space, or it is empty.**

That works because every vector north stores is derived data - tool
descriptions, code chunks, document chunks are all still on disk - so a space
change discards a cache rather than losing anything. The alternative, tagging
each row with its model and mixing spaces at read time, would keep stale vectors
around to be filtered on every query for no benefit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vector_space (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    model      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_vector_space(db_path: Path, model: str, tables: Iterable[str]) -> bool:
    """Record *model* as this store's embedding space, clearing it on a change.

    Returns True when the store was reset, so the caller can re-index. A store
    that has never been stamped is adopted as-is: it was written by whatever
    model was configured at the time, and re-deriving everything on first upgrade
    would be a worse trade than trusting the rows already there.

    Never raises - a store that cannot be stamped keeps working exactly as it did
    before, which is the behaviour this replaces.
    """
    if not model:
        return False
    table_names = [t for t in tables if t]
    try:
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            row = conn.execute("SELECT model FROM vector_space WHERE id = 1").fetchone()
            current = row["model"] if row is not None else None
            if current == model:
                return False
            if current is not None:
                for table in table_names:
                    conn.execute(f"DELETE FROM {table}")  # noqa: S608 - names are module constants
                logger.info(
                    "Embedding model changed (%s -> %s) - cleared %s in %s so they can be rebuilt",
                    current,
                    model,
                    ", ".join(table_names),
                    db_path.name,
                )
            conn.execute(
                "INSERT INTO vector_space (id, model, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT (id) DO UPDATE SET model = excluded.model, updated_at = CURRENT_TIMESTAMP",
                (model,),
            )
            return current is not None
    except Exception:
        logger.warning("Could not stamp the embedding space of %s", db_path, exc_info=True)
        return False


def ensure_vector_space_reembed(db_path: Path, model: str, tables: Iterable[tuple[str, str]]) -> bool:
    """Stamp *model* on a store whose rows are the source of truth, clearing vectors only.

    ``ensure_vector_space`` deletes rows outright, which is right for derived
    caches: tool descriptions, code chunks and document chunks all still exist on
    disk, so a space change discards a rebuildable index. Facts and episodes are
    not like that - the documents they were extracted from may be long gone, so
    the rows *are* the data. Here a space change clears the embedding column and
    leaves the content alone; the caller re-embeds it in place.

    *tables* is (table, embedding column) pairs.

    Unlike ``ensure_vector_space`` an unstamped store is **not** adopted as-is.
    It was written by an unknown model, and re-embedding a few hundred rows costs
    about a second - where being wrong means every query compares vectors of
    different lengths, which raises inside the cosine scan and silently recalls
    nothing at all. Adopting is the cheap bet only when the downside is small.

    Returns True when vectors were cleared, so the caller can backfill.
    Never raises.
    """
    if not model:
        return False
    pairs = [(t, c) for t, c in tables if t and c]
    try:
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            row = conn.execute("SELECT model FROM vector_space WHERE id = 1").fetchone()
            current = row["model"] if row is not None else None
            if current == model:
                return False
            cleared = False
            for table, column in pairs:
                # An empty store has nothing to clear and nothing to backfill;
                # only report a reset when rows actually lost their vectors.
                changed = conn.execute(
                    f"UPDATE {table} SET {column} = NULL "  # noqa: S608 - names are module constants
                    f"WHERE {column} IS NOT NULL AND {column} != '' AND {column} != '[]'"
                ).rowcount
                cleared = cleared or changed > 0
            if cleared:
                logger.info(
                    "Embedding model changed (%s -> %s) - cleared the vectors in %s in %s, "
                    "content kept and queued for re-embedding",
                    current or "unstamped",
                    model,
                    ", ".join(t for t, _ in pairs),
                    db_path.name,
                )
            conn.execute(
                "INSERT INTO vector_space (id, model, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT (id) DO UPDATE SET model = excluded.model, updated_at = CURRENT_TIMESTAMP",
                (model,),
            )
            return cleared
    except Exception:
        logger.warning("Could not stamp the embedding space of %s", db_path, exc_info=True)
        return False
