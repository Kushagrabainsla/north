"""Daily snapshots of the memory stores.

north backed up its context documents by copying `*.md` out of the context
directory. That directory stopped holding anything when the documents moved into
SQLite, so the backup silently copied nothing for as long as it has existed - and
`facts.db`, the store this system is actually about, was never in scope at all.

The gap is not theoretical. A wipe of ~/.north took 220 extracted facts with it,
and they were only recoverable because a copy happened to exist elsewhere.
Rebuilding them from source files costs hours of LLM calls and does not return
the same set, so the store is not the cheap, re-derivable cache the rest of
north's vector stores are. It is the data.

Snapshots use SQLite's online backup API rather than a file copy: these databases
run in WAL mode with a live writer attached, where copying the file alone can
capture a torn page and silently lose the tail of the log.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from utils.time import utcnow

logger = logging.getLogger(__name__)

# What is worth keeping. Every other store under north_home is derived - vectors
# rebuild from their source, the model catalogue re-fetches, task rows expire -
# and would only make the snapshot bigger and slower to write.
_BACKED_UP: tuple[str, ...] = (
    "facts.db",  # extracted personal facts - expensive and non-deterministic to rebuild
    "episodic.db",  # task history summaries; a projection of the ledger, but a costly one
    "memory.db",  # context documents, including the user profile
    "ledger.db",  # the audit log everything else is derived from
)

# Enough to survive a bad write going unnoticed over a long weekend.
_KEEP_SNAPSHOTS: int = 7


def snapshot_memory(north_home: Path, keep: int = _KEEP_SNAPSHOTS) -> int:
    """Copy each memory store into a timestamped folder. Returns files written.

    Never raises: a failed backup must not take down the caller that scheduled
    it, and a partial snapshot is still better than none.
    """
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest_dir = north_home / "backups" / stamp
    written = 0
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Memory backup: could not create %s", dest_dir, exc_info=True)
        return 0

    for name in _BACKED_UP:
        source = north_home / name
        if not source.is_file():
            continue
        try:
            _copy_database(source, dest_dir / name)
            written += 1
        except Exception:
            logger.warning("Memory backup: could not snapshot %s", name, exc_info=True)

    if written:
        logger.info("Memory backup: wrote %d store(s) to %s", written, dest_dir)
        _prune(north_home / "backups", keep)
    else:
        # An empty directory left behind reads as a snapshot that exists.
        with _suppressed():
            dest_dir.rmdir()
    return written


def _copy_database(source: Path, dest: Path) -> None:
    """Online-backup *source* to *dest*, consistent even under a live writer."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _prune(root: Path, keep: int) -> None:
    """Keep the newest *keep* snapshot folders, delete the rest."""
    try:
        snapshots = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return
    for stale in snapshots[keep:]:
        with _suppressed():
            for child in stale.iterdir():
                child.unlink()
            stale.rmdir()


class _suppressed:
    """Swallow cleanup errors - a snapshot that cannot be pruned is not a failure."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
