import sqlite3
from pathlib import Path

import pytest

from utils.db import close_all_pools, open_db_connection


def test_open_db_connection_pooling_and_pragmas(tmp_path: Path):
    db_path = tmp_path / "test.db"
    with open_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('Alice')")

    # Second open returns pooled connection
    with open_db_connection(db_path) as conn:
        row = conn.execute("SELECT name FROM users WHERE id = 1").fetchone()
        assert row["name"] == "Alice"
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_open_db_connection_nested_rollback(tmp_path: Path):
    db_path = tmp_path / "test_nested.db"
    with open_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")

    # Test that an exception in outer block rolls back everything, including nested block
    with pytest.raises(RuntimeError):
        with open_db_connection(db_path) as conn1:
            conn1.execute("INSERT INTO items (title) VALUES ('Outer 1')")
            with open_db_connection(db_path) as conn2:
                conn2.execute("INSERT INTO items (title) VALUES ('Inner')")
            raise RuntimeError("Outer failure")

    with open_db_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count == 0


def test_open_db_connection_nested_commit(tmp_path: Path):
    db_path = tmp_path / "test_nested_commit.db"
    with open_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")

    with open_db_connection(db_path) as conn1:
        conn1.execute("INSERT INTO items (title) VALUES ('Outer')")
        with open_db_connection(db_path) as conn2:
            conn2.execute("INSERT INTO items (title) VALUES ('Inner')")

    with open_db_connection(db_path) as conn:
        rows = [r[0] for r in conn.execute("SELECT title FROM items ORDER BY id ASC").fetchall()]
        assert rows == ["Outer", "Inner"]


def test_close_all_pools(tmp_path: Path):
    db_path = tmp_path / "test_close.db"
    with open_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (42)")

    close_all_pools()

    # Re-opening establishes a valid fresh connection
    with open_db_connection(db_path) as conn:
        val = conn.execute("SELECT x FROM t").fetchone()[0]
        assert val == 42
