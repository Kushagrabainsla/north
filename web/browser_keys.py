"""Browsers signed in to this north, remembered across restarts.

A browser signs in once, with the one-time link `north web` opens (see
`config.security.issue_pairing`), and keeps a key in an httponly cookie that
other OS users cannot read. The key renews the dashboard session, which lives
only as long as the server does. Only a hash of each key is stored.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from utils.db import open_db_connection

BROWSER_KEY_COOKIE = "north_browser"
BROWSER_KEY_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_keys (
    key_hash     TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    last_used_at REAL NOT NULL
)
"""


def _path(home: Path) -> Path:
    return home / "web_access.db"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def remember_browser(home: Path) -> str:
    """A new browser key. Only its hash is stored."""
    key = secrets.token_urlsafe(32)
    now = time.time()
    with open_db_connection(_path(home)) as conn:
        conn.execute(_SCHEMA)
        conn.execute("INSERT INTO browser_keys VALUES (?, ?, ?)", (_hash(key), now, now))
    return key


def known_browser(home: Path, key: str) -> bool:
    """Whether *key* belongs to a browser signed in here and used within the last year."""
    if not key:
        return False
    now = time.time()
    with open_db_connection(_path(home)) as conn:
        conn.execute(_SCHEMA)
        return (
            conn.execute(
                "UPDATE browser_keys SET last_used_at = ? WHERE key_hash = ? AND last_used_at > ?",
                (now, _hash(key), now - BROWSER_KEY_MAX_AGE_SECONDS),
            ).rowcount
            > 0
        )
