from __future__ import annotations

import sqlite3

from memory.backup import snapshot_memory


def test_snapshot_includes_session_store(tmp_path) -> None:
    names = ("facts.db", "episodic.db", "memory.db", "ledger.db", "web.db")
    for name in names:
        with sqlite3.connect(tmp_path / name) as conn:
            conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            conn.execute("INSERT INTO marker VALUES (?)", (name,))

    written = snapshot_memory(tmp_path)

    assert written == len(names)
    snapshots = list((tmp_path / "backups").iterdir())
    assert len(snapshots) == 1
    for name in names:
        with sqlite3.connect(snapshots[0] / name) as conn:
            assert conn.execute("SELECT value FROM marker").fetchone() == (name,)
