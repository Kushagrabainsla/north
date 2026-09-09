"""What north has already offered you, so it does not offer it again tomorrow.

A daily flow that finds candidates will find the same ones every day. Without
this it re-proposes them forever, which is the single most likely way the review
queue becomes annoying enough to turn off.

Matching on the raw URL does not work. The same job posting arrives with
different tracking parameters, behind a redirect, and cross-posted to several
boards - three URLs, one thing you already said no to. So the key is normalized
first, and a second natural key (company, role) catches the cross-posting the
URL never will.

Lives in jobs.db because a proposed item shares a lifecycle with the job that
follows it, and ~/.north already holds fifteen SQLite files.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    first_seen DATETIME NOT NULL,
    PRIMARY KEY (source, key)
)
"""

# Query parameters that identify the referrer rather than the thing. Stripping
# them is what makes two copies of one posting the same key.
_TRACKING_PARAMS = re.compile(r"(?i)^(utm_|ref$|referrer$|source$|src$|fbclid$|gclid$|mc_|campaign)")
_WHITESPACE = re.compile(r"\s+")


def normalize_key(raw: str) -> str:
    """A stable identity for *raw*, whether it is a URL or a plain string."""
    text = raw.strip().lower()
    if not text:
        return ""
    if "://" in text:
        parts = urlsplit(text)
        kept = [pair for pair in parts.query.split("&") if pair and not _TRACKING_PARAMS.match(pair.split("=", 1)[0])]
        host = parts.netloc.removeprefix("www.")
        path = parts.path.rstrip("/")
        return urlunsplit(("", host, path, "&".join(sorted(kept)), ""))
    return _WHITESPACE.sub(" ", text)


def natural_key(*parts: str) -> str:
    """A key from what the thing *is* rather than where it was found.

    (company, role) catches the same posting on three job boards, which no
    amount of URL normalising can.
    """
    return "|".join(_WHITESPACE.sub(" ", p.strip().lower()) for p in parts if p and p.strip())


class SeenStore:
    """Remembers which candidates a source has already put in front of you."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)

    def seen(self, source: str, *keys: str) -> bool:
        """Whether *source* has already offered something under any of *keys*.

        Several keys because one item has more than one identity - its URL and
        its (company, role) - and matching either means you have seen it.
        """
        candidates = [k for k in (normalize_key(key) for key in keys) if k]
        if not candidates:
            return False
        placeholders = ",".join("?" for _ in candidates)
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                f"SELECT 1 FROM seen WHERE source = ? AND key IN ({placeholders}) LIMIT 1",
                (source, *candidates),
            ).fetchone()
        return row is not None

    def remember(self, source: str, *keys: str) -> None:
        """Record every identity of one item, so any of them matches later."""
        now = datetime.now(UTC).isoformat()
        rows = [(source, k, now) for k in (normalize_key(key) for key in keys) if k]
        if not rows:
            return
        with open_db_connection(self._db_path) as conn:
            conn.executemany("INSERT OR IGNORE INTO seen (source, key, first_seen) VALUES (?, ?, ?)", rows)

    def forget(self, source: str, key: str) -> bool:
        """Let one item be offered again - the undo for a wrong dedupe."""
        with open_db_connection(self._db_path) as conn:
            removed = conn.execute(
                "DELETE FROM seen WHERE source = ? AND key = ?", (source, normalize_key(key))
            ).rowcount
        return bool(removed)

    def count(self, source: str) -> int:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM seen WHERE source = ?", (source,)).fetchone()
        return int(row["n"]) if row else 0
