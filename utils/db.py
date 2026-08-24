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

# Thread-local storage: each thread caches one connection per canonical db_path,
# and tracks transaction nesting depth.
_local = threading.local()
# Global registry so close_all_pools() can reach every cached connection.
_all_connections_lock = threading.Lock()
_all_connections: list[sqlite3.Connection] = []


def _get_pooled_connection(db_path: Path) -> tuple[str, sqlite3.Connection]:
    """Return (canonical_key, connection) for *db_path* in the current thread.

    On first access per (thread, path), opens the connection, runs PRAGMAs once,
    and caches it. Subsequent calls return the same connection.
    """
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    key = str(db_path.resolve())
    conn = cache.get(key)
    if conn is not None:
        try:
            # Quick liveness check — will raise if the connection was closed
            # externally (e.g. by close_all_pools during a hot reload).
            conn.execute("SELECT 1")
            return key, conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            cache.pop(key, None)
            with _all_connections_lock, contextlib.suppress(ValueError):
                _all_connections.remove(conn)
    conn = sqlite3.connect(key)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    cache[key] = conn
    _local.conns = cache
    with _all_connections_lock:
        _all_connections.append(conn)
    return key, conn


@contextlib.contextmanager
def open_db_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a pooled SQLite connection with WAL mode, normal sync, foreign keys, and Row factory.

    Every SQLite-backed module in north (ledger, jobs, tools, tasks) opens
    connections through this single helper (docs/CODING_STYLE.md Section 11.1).

    Context manager: commits on clean exit of the outermost context, rolls back on exception.
    Re-entrant/nested calls within the same thread reuse the active transaction without
    prematurely committing outer blocks. Connections are **not** closed on exit — they are
    reused across calls within the same thread. Use ``close_all_pools()`` for cleanup at shutdown.
    """
    key, conn = _get_pooled_connection(db_path)
    depths: dict[str, int] = getattr(_local, "depths", None) or {}
    depth = depths.get(key, 0) + 1
    depths[key] = depth
    _local.depths = depths
    try:
        yield conn
        if depth == 1:
            conn.commit()
    except BaseException:
        if depth == 1:
            conn.rollback()
        raise
    finally:
        current_depths: dict[str, int] = getattr(_local, "depths", None) or {}
        new_depth = max(0, current_depths.get(key, 1) - 1)
        if new_depth == 0:
            current_depths.pop(key, None)
        else:
            current_depths[key] = new_depth
        _local.depths = current_depths


def close_all_pools() -> None:
    """Close every cached connection across all threads.

    Call on application shutdown or before moving/replacing DB files.
    """
    with _all_connections_lock:
        for conn in list(_all_connections):
            with contextlib.suppress(Exception):
                conn.close()
        _all_connections.clear()


atexit.register(close_all_pools)

