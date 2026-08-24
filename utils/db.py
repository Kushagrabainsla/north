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
# Global registry keyed by thread id so close_all_pools() can reach all cached
# connections across threads, with automatic cleanup of terminated thread connections.
_registry_lock = threading.Lock()
_thread_conns: dict[int, dict[str, sqlite3.Connection]] = {}


def _prune_dead_threads_locked() -> None:
    """Close and evict connections belonging to threads that are no longer alive."""
    alive_ids = {t.ident for t in threading.enumerate() if t.ident is not None}
    dead_ids = [tid for tid in _thread_conns if tid not in alive_ids]
    for tid in dead_ids:
        for conn in _thread_conns.pop(tid, {}).values():
            with contextlib.suppress(Exception):
                conn.close()


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
            with _registry_lock:
                tid = threading.get_ident()
                if tid in _thread_conns:
                    _thread_conns[tid].pop(key, None)
    conn = sqlite3.connect(key)
    with contextlib.suppress(Exception):
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    cache[key] = conn
    _local.conns = cache
    with _registry_lock:
        _prune_dead_threads_locked()
        tid = threading.get_ident()
        if tid not in _thread_conns:
            _thread_conns[tid] = {}
        _thread_conns[tid][key] = conn
    return key, conn


@contextlib.contextmanager
def open_db_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a pooled SQLite connection with WAL mode, normal sync, foreign keys, and Row factory.

    Every SQLite-backed module in north (ledger, jobs, tools, tasks, memory) opens
    connections through this single helper (docs/CODING_STYLE.md Section 11.1).

    Context manager: commits on clean exit of the outermost context, rolls back on exception.
    Re-entrant/nested calls within the same thread use SAVEPOINTs so inner errors roll back
    only their sub-transactions without prematurely committing outer blocks or leaving
    dirty uncommitted state. Connections are **not** closed on exit — they are reused
    across calls within the same thread. Use ``close_all_pools()`` for cleanup at shutdown.
    """
    key, conn = _get_pooled_connection(db_path)
    depths: dict[str, int] = getattr(_local, "depths", None) or {}
    depth = depths.get(key, 0) + 1
    depths[key] = depth
    _local.depths = depths
    sp_name = f"north_sp_{depth}"
    if depth > 1:
        conn.execute(f"SAVEPOINT {sp_name}")
    try:
        yield conn
        if depth == 1:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {sp_name}")
    except BaseException:
        if depth == 1:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
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
    with _registry_lock:
        for conns in _thread_conns.values():
            for conn in conns.values():
                with contextlib.suppress(Exception):
                    conn.close()
        _thread_conns.clear()


atexit.register(close_all_pools)


