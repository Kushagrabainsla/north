"""SQLite connection helper shared by every *.db file under ~/.north/."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, normal sync, foreign keys, and Row factory.

    Every SQLite-backed module in north (ledger, jobs, tools, tasks) opens
    connections through this single helper (docs/CODING_STYLE.md Section 11.1).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn
