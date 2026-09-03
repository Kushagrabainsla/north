"""One embedding space per store - the invariant that makes similarity mean something."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from utils.vector_space import ensure_vector_space


def _store(tmp_path: Path, rows: int = 2) -> Path:
    db = tmp_path / "vectors.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE vecs (id TEXT PRIMARY KEY, embedding TEXT)")
        conn.execute("CREATE TABLE sources (id TEXT PRIMARY KEY)")
        for i in range(rows):
            conn.execute("INSERT INTO vecs VALUES (?, '[0.1,0.2]')", (f"r{i}",))
            conn.execute("INSERT INTO sources VALUES (?)", (f"r{i}",))
    return db


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_an_unstamped_store_is_adopted_rather_than_wiped(tmp_path: Path) -> None:
    """Existing rows were written by whatever model was configured then.

    Re-deriving every index the first time north upgrades would be a worse trade
    than trusting what is already there.
    """
    db = _store(tmp_path)
    assert ensure_vector_space(db, "model-a", ("vecs",)) is False
    assert _count(db, "vecs") == 2


def test_the_same_model_changes_nothing(tmp_path: Path) -> None:
    db = _store(tmp_path)
    ensure_vector_space(db, "model-a", ("vecs",))
    assert ensure_vector_space(db, "model-a", ("vecs",)) is False
    assert _count(db, "vecs") == 2


def test_a_changed_model_clears_the_vectors(tmp_path: Path) -> None:
    """Otherwise cosine similarity silently compares two different spaces."""
    db = _store(tmp_path)
    ensure_vector_space(db, "model-a", ("vecs",))
    assert ensure_vector_space(db, "model-b", ("vecs",)) is True
    assert _count(db, "vecs") == 0


def test_every_named_table_is_cleared(tmp_path: Path) -> None:
    """The code index must drop its file hashes with its chunks.

    Clearing chunks alone would leave every file looking already-indexed, and the
    index would stay permanently empty.
    """
    db = _store(tmp_path)
    ensure_vector_space(db, "model-a", ("vecs", "sources"))
    ensure_vector_space(db, "model-b", ("vecs", "sources"))
    assert _count(db, "vecs") == 0
    assert _count(db, "sources") == 0


def test_switching_back_clears_again(tmp_path: Path) -> None:
    db = _store(tmp_path)
    ensure_vector_space(db, "model-a", ("vecs",))
    ensure_vector_space(db, "model-b", ("vecs",))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO vecs VALUES ('new', '[0.3]')")
    assert ensure_vector_space(db, "model-a", ("vecs",)) is True
    assert _count(db, "vecs") == 0


def test_an_unknown_model_is_never_acted_on(tmp_path: Path) -> None:
    """No model id means "we do not know" - which must not wipe anything."""
    db = _store(tmp_path)
    ensure_vector_space(db, "model-a", ("vecs",))
    assert ensure_vector_space(db, "", ("vecs",)) is False
    assert _count(db, "vecs") == 2


def test_an_unusable_store_never_raises(tmp_path: Path) -> None:
    """Stamping is a safeguard; failing to stamp must not break indexing."""
    assert ensure_vector_space(tmp_path / "missing" / "x.db", "model-a", ("nope",)) is False
