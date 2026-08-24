"""SQLite connection helper shared by every *.db file under ~/.north/.

Provides a thread-local connection pool so callers reuse the same connection
across operations within a thread, eliminating the overhead of opening a fresh
connection + running 4 PRAGMAs on every single query.  The context-manager API
(``open_db_connection``) is unchanged — callers are not affected.
"""

from __future__ import annotations

import atexit
import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

# Thread-local storage: each thread caches one connection per db_path.
_local = threading.local()
# Global registry so close_all_pools() can reach every cached connection.
_all_connections_lock = threading.Lock()
_all_connections: list[sqlite3.Connection] = []


def _get_pooled_connection(db_path: Path) -> sqlite3.Connection:
    """Return a cached connection for *db_path* in the current thread.

    On first access per (thread, path), opens the connection, runs PRAGMAs once,
    and caches it.  Subsequent calls return the same connection.
    """
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    key = str(db_path)
    conn = cache.get(key)
    if conn is not None:
        try:
            # Quick liveness check — will raise if the connection was closed
            # externally (e.g. by close_all_pools during a hot reload).
            conn.execute("SELECT 1")
            return conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            cache.pop(key, None)
            with _all_connections_lock:
                try:
                    _all_connections.remove(conn)
                except ValueError:
                    pass
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    cache[key] = conn
    _local.conns = cache
    with _all_connections_lock:
        _all_connections.append(conn)
    return conn


@contextlib.contextmanager
def open_db_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a pooled SQLite connection with WAL mode, normal sync, foreign keys, and Row factory.

    Every SQLite-backed module in north (ledger, jobs, tools, tasks) opens
    connections through this single helper (docs/CODING_STYLE.md Section 11.1).

    Context manager: commits on clean exit, rolls back on exception.
    Connections are **not** closed on exit — they are reused across calls within
    the same thread.  Use ``close_all_pools()`` for explicit cleanup at shutdown.
    """
    conn = _get_pooled_connection(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def close_all_pools() -> None:
    """Close every cached connection across all threads.

    Call on application shutdown or before moving/replacing DB files.
    """
    with _all_connections_lock:
        for conn in _all_connections:
            with contextlib.suppress(Exception):
                conn.close()
        _all_connections.clear()


atexit.register(close_all_pools)
