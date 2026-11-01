"""What a person said no to in each task, so the "no" holds for the rest of it.

After a rejected shell command the agent read the same folder with `read_file`
and `list_dir`, which never ask, and answered as if nobody had objected (issue
#76, finding M1). A rejection now covers every path the rejected action named,
for the rest of that task, reads included (CODING_STYLE §7, "What a decision
covers"). Reads stay otherwise unbounded (§16.1.1).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rejected_paths (
    task_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    rejected_at TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
)
"""


class RejectionLog:
    """The paths each task was told no about."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)
        # task id -> [(path, summary)], read once per task and kept in step with record().
        self._cache: dict[str, list[tuple[str, str]]] = {}

    def record(self, task_id: str, paths: Iterable[Path], summary: str) -> None:
        rows = [(task_id, str(path), summary[:500], datetime.now(UTC).isoformat()) for path in paths]
        if not rows:
            return
        try:
            with open_db_connection(self._db_path) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO rejected_paths (task_id, path, summary, rejected_at) VALUES (?, ?, ?, ?)",
                    rows,
                )
        except Exception:
            logger.warning("RejectionLog: could not record a rejection for task %s", task_id, exc_info=True)
        self._cache.pop(task_id, None)

    def covering(self, task_id: str, path: Path) -> tuple[str, str] | None:
        """The rejected path and action that cover *path* in this task, or None."""
        target = str(path)
        for rejected, summary in self._rejected(task_id):
            if target == rejected or target.startswith(rejected.rstrip("/") + "/"):
                return rejected, summary
        return None

    def _rejected(self, task_id: str) -> list[tuple[str, str]]:
        if task_id not in self._cache:
            try:
                with open_db_connection(self._db_path) as conn:
                    rows = conn.execute(
                        "SELECT path, summary FROM rejected_paths WHERE task_id = ?", (task_id,)
                    ).fetchall()
            except Exception:
                logger.warning("RejectionLog: could not read rejections for task %s", task_id, exc_info=True)
                return []
            self._cache[task_id] = [(row["path"], row["summary"]) for row in rows]
        return self._cache[task_id]
