"""SQLite connection helper shared by every *.db file under ~/.north/."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def open_db_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with WAL mode, normal sync, foreign keys, and Row factory.

    Every SQLite-backed module in north (ledger, jobs, tools, tasks) opens
    connections through this single helper (docs/CODING_STYLE.md Section 11.1).

    Context manager: commits on clean exit, rolls back on exception, and always
    closes the connection.  A bare ``sqlite3.Connection`` used via ``with`` only
    manages the transaction — it never closes, which leaks the file handle and
    WAL state until garbage collection.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
