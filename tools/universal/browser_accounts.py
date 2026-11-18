"""Which of your browser profiles is signed in where, as a person remembers it.

You know that your school LinkedIn is in one Chrome profile and your personal mail in
another, from what you set up and what you saw. North learns it the same way, never from
a password or a cookie:

- What it saw: each time north opens a site in a profile, a fixed script tells whether
  the page asks to sign in, and which of the profile's own names it shows (the Google
  account Chrome records for the profile). The latest result per site and profile is kept.
- Where you browse: Chrome's history of each existing profile, read as site names and
  visit counts only, at most once a day. It stays on this Mac and is never sent to a model.

`pick` names the profile to use for a site when one answer stands out; with none, north
asks you. Being signed in somewhere is not permission: what north may do there is still
the approval layer's to decide.
"""

from __future__ import annotations

import datetime
import json
import logging
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from utils.db import open_db_connection
from utils.time import format_timestamp, utcnow

if TYPE_CHECKING:
    from config.browser_profiles import BrowserProfile

logger = logging.getLogger(__name__)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS seen (
        site TEXT NOT NULL, profile_id TEXT NOT NULL, signed_in INTEGER NOT NULL,
        account TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL, PRIMARY KEY (site, profile_id))""",
    """CREATE TABLE IF NOT EXISTS visits (
        site TEXT NOT NULL, profile_id TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (site, profile_id))""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
# History counts as a pick only when one profile clearly holds the site: enough visits, nearly all of them.
_HISTORY_MIN_VISITS = 5
_HISTORY_SHARE = 0.8
_HISTORY_EVERY = datetime.timedelta(days=1)
# Sites listed per profile in list_profiles: enough to choose by, few tokens.
_LISTED_SITES = 12


def site_of(url: str) -> str:
    """The site a URL is on, as north names it everywhere: its host without "www."."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


@dataclass(frozen=True)
class Pick:
    """The profile to use for a site, and why, in words for the agent and the card."""

    profile_id: str
    why: str


def chrome_identity(profile: BrowserProfile) -> list[str]:
    """The names an existing profile is known by: the Google account's name and email, as Chrome records them."""
    if profile.context != "existing" or not profile.data_directory:
        return []
    try:
        state = json.loads((Path(profile.data_directory) / "Local State").read_text(encoding="utf-8"))
        info = state["profile"]["info_cache"][profile.profile_directory]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [str(info[key]).strip() for key in ("gaia_name", "user_name") if str(info.get(key) or "").strip()]


def history_sites(profile: BrowserProfile) -> Counter[str]:
    """Visits per site in an existing profile's Chrome history: site names and counts, nothing else.

    Chrome holds the file open, so it is copied first and the copy deleted.
    """
    if profile.context != "existing" or not profile.data_directory:
        return Counter()
    source = Path(profile.data_directory) / profile.profile_directory / "History"
    if not source.is_file():
        return Counter()
    with tempfile.TemporaryDirectory(prefix="north-history-") as folder:
        copy = Path(folder) / "History"
        shutil.copyfile(source, copy)
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT url, visit_count FROM urls WHERE visit_count > 0").fetchall()
        finally:
            connection.close()
    visits: Counter[str] = Counter()
    for url, count in rows:
        if (site := site_of(str(url))) and urlparse(str(url)).scheme in {"http", "https"}:
            visits[site] += int(count)
    return visits


class BrowserAccounts:
    """The map of where each profile is signed in, and where you browse: one SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(db_path) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def record(self, url: str, profile_id: str, *, signed_in: bool, account: str = "") -> None:
        """Keep what north just saw on *url* in *profile_id*. An account seen before stays until another is seen."""
        if not (site := site_of(url)):
            return
        with open_db_connection(self._db_path) as connection:
            connection.execute(
                "INSERT INTO seen (site, profile_id, signed_in, account, seen_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (site, profile_id) DO UPDATE SET signed_in = excluded.signed_in, "
                "account = CASE WHEN excluded.account != '' THEN excluded.account ELSE seen.account END, "
                "seen_at = excluded.seen_at",
                (site, profile_id, int(signed_in), account, format_timestamp(utcnow())),
            )

    def signed_in_sites(self, profile_id: str) -> list[str]:
        """The sites north last saw *profile_id* signed in on, newest first, each with its account when known."""
        with open_db_connection(self._db_path) as connection:
            rows = connection.execute(
                "SELECT site, account, seen_at FROM seen WHERE profile_id = ? AND signed_in = 1 "
                "ORDER BY seen_at DESC LIMIT ?",
                (profile_id, _LISTED_SITES),
            ).fetchall()
        return [f"{row['site']} as {row['account']}" if row["account"] else row["site"] for row in rows]

    def pick(self, url: str, profiles: list[BrowserProfile]) -> Pick | None:
        """The profile to use for *url*'s site, or None when no answer stands out and north should ask.

        Seen signed in beats history: exactly one enabled profile seen signed in there is the pick. With
        none seen signed in, the one profile where nearly all your visits to the site happened, unless
        north saw it asking to sign in there.
        """
        if not (site := site_of(url)):
            return None
        enabled = {profile.id: profile for profile in profiles if profile.enabled}
        with open_db_connection(self._db_path) as connection:
            seen = connection.execute(
                "SELECT profile_id, signed_in, account, seen_at FROM seen WHERE site = ?", (site,)
            ).fetchall()
            visits = connection.execute("SELECT profile_id, count FROM visits WHERE site = ?", (site,)).fetchall()
        signed_in = [row for row in seen if row["signed_in"] and row["profile_id"] in enabled]
        if len(signed_in) == 1:
            row = signed_in[0]
            account = f" as {row['account']}" if row["account"] else ""
            day = str(row["seen_at"])[:10]
            return Pick(row["profile_id"], f"signed in on {site}{account}, seen {day}")
        if signed_in:
            return None  # signed in in several profiles: which account the task wants is yours to say
        signed_out = {row["profile_id"] for row in seen if not row["signed_in"]}
        counts = {row["profile_id"]: int(row["count"]) for row in visits if row["profile_id"] in enabled}
        total = sum(counts.values())
        if not counts or total < _HISTORY_MIN_VISITS:
            return None
        profile_id, count = max(counts.items(), key=lambda item: item[1])
        if count / total < _HISTORY_SHARE or profile_id in signed_out:
            return None
        return Pick(profile_id, f"you visit {site} in this profile ({count} of {total} visits)")

    def refresh_history(self, profiles: list[BrowserProfile]) -> bool:
        """Read where you browse, from each existing profile's history, at most once a day. True when read."""
        with open_db_connection(self._db_path) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = 'history_read_at'").fetchone()
        if row and utcnow() - datetime.datetime.fromisoformat(row["value"]) < _HISTORY_EVERY:
            return False
        rows: list[tuple[str, str, int]] = []
        for profile in profiles:
            try:
                rows += [(site, profile.id, count) for site, count in history_sites(profile).items()]
            except (OSError, sqlite3.Error):
                logger.warning("Could not read the browser history of %s", profile.name, exc_info=True)
        with open_db_connection(self._db_path) as connection:
            connection.execute("DELETE FROM visits")
            connection.executemany("INSERT INTO visits (site, profile_id, count) VALUES (?, ?, ?)", rows)
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('history_read_at', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (format_timestamp(utcnow()),),
            )
        return True
