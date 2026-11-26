"""How often each memory item was loaded into a task, and how often the task used it (#85, #91).

The counts come from the memory_used judgement of each finished run. Recall reads
them back as a small score next to similarity: an item that tasks keep loading
and never use drifts down, and one they use drifts up.

Only a use rate is kept, no time fade. On 196 replayed tasks (experiments/
memory_recall), similarity + 0.1 * (rate - 0.5) found more of the items a task
needed: recall 0.62 -> 0.68 on all evaluated tasks, +0.12 on held-out ones (95%
interval 0.04 to 0.20). A 60-day fade, as #91 first proposed, did not win on
held-out tasks, and an exploration place found nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from utils.db import open_db_connection
from utils.time import format_timestamp, utcnow

# How much the use rate moves an item, in cosine-similarity units. Similarity
# stays in charge: the whole range of the term is 0.1, and an item is still kept
# only if its similarity clears the store's floor.
USE_WEIGHT = 0.1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_use (
    item_id    TEXT PRIMARY KEY,
    loaded     INTEGER NOT NULL DEFAULT 0,
    used       INTEGER NOT NULL DEFAULT 0,
    last_used  TEXT
)
"""


class MemoryUseStore:
    """Loaded and used counts per memory item id ("fact:<id>", "episode:<id>", "profile:<heading>")."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(db_path) as conn:
            conn.execute(_SCHEMA)

    async def record(self, loaded: Iterable[str], used: Iterable[str]) -> None:
        await asyncio.to_thread(self._record_sync, list(loaded), set(used))

    def _record_sync(self, loaded: list[str], used: set[str]) -> None:
        now = format_timestamp(utcnow())
        with open_db_connection(self._db_path) as conn:
            for item_id in dict.fromkeys(loaded):
                hit = item_id in used
                conn.execute(
                    "INSERT INTO memory_use(item_id, loaded, used, last_used) VALUES (?, 1, ?, ?) "
                    "ON CONFLICT(item_id) DO UPDATE SET loaded = loaded + 1, used = used + excluded.used, "
                    "last_used = COALESCE(excluded.last_used, last_used)",
                    (item_id, int(hit), now if hit else None),
                )

    def boosts(self, item_ids: Iterable[str]) -> dict[str, float]:
        """The score each item gets next to its similarity: USE_WEIGHT * (rate - 0.5).

        The rate is (used + 1) / (loaded + 2), so an item never loaded sits at 0.5
        and gets nothing: recall is unchanged until there is data.
        """
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {}
        out = dict.fromkeys(ids, 0.0)
        with open_db_connection(self._db_path) as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                rows = conn.execute(
                    f"SELECT item_id, loaded, used FROM memory_use WHERE item_id IN ({','.join('?' * len(chunk))})",  # noqa: S608
                    chunk,
                ).fetchall()
                for row in rows:
                    rate = (row["used"] + 1) / (row["loaded"] + 2)
                    out[row["item_id"]] = USE_WEIGHT * (rate - 0.5)
        return out

    async def used_share(self) -> dict[str, dict[str, int]]:
        """Loaded and used totals per kind of item, for the report of #85."""
        return await asyncio.to_thread(self._used_share_sync)

    def _used_share_sync(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with open_db_connection(self._db_path) as conn:
            for row in conn.execute("SELECT item_id, loaded, used FROM memory_use"):
                kind = row["item_id"].split(":", 1)[0]
                entry = out.setdefault(kind, {"loaded": 0, "used": 0})
                entry["loaded"] += row["loaded"]
                entry["used"] += row["used"]
        return out
