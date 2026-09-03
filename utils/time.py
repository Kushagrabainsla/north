"""Standardized date and time utilities.

north stores every instant as a Unix epoch (seconds, UTC) and shows every
instant in the machine's local zone. Storage is unambiguous, display is what
the user actually lives in - and nothing in between has to guess which one a
bare "07:00" meant.

See docs/CODING_STYLE.md Section 5.2.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# How an instant is rendered for a person: local wall clock plus the zone, so a
# briefing that fired at 08:00 IST never reads as an unlabelled "08:00".
LOCAL_DISPLAY_FORMAT = "%Y-%m-%d %H:%M %Z"

_ZONEINFO_MARKER = "zoneinfo/"


def utcnow() -> datetime.datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def localnow() -> datetime.datetime:
    """Return the current timezone-aware local datetime.

    Use this instead of datetime.now().astimezone() so all call sites go through
    a single canonical implementation and the UTC→local conversion is consistent.
    """
    return utcnow().astimezone(local_timezone())


def now_epoch() -> float:
    """Return the current instant as Unix epoch seconds."""
    return utcnow().timestamp()


def to_epoch(dt: datetime.datetime) -> float:
    """Return *dt* as Unix epoch seconds; a naive datetime is read as local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_timezone())
    return dt.timestamp()


def from_epoch(epoch: float) -> datetime.datetime:
    """Return the timezone-aware UTC datetime for *epoch* seconds."""
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC)


def epoch_to_local(epoch: float) -> datetime.datetime:
    """Return the timezone-aware local datetime for *epoch* seconds."""
    return from_epoch(epoch).astimezone(local_timezone())


def local_timezone_name() -> str:
    """Return the machine's IANA zone name, e.g. "Asia/Kolkata".

    Falls back to a fixed-offset label ("UTC+05:30") on a machine whose zone
    cannot be resolved to an IANA name, which is still unambiguous to read.
    """
    env = os.environ.get("TZ")
    if env and _is_known_zone(env):
        return env
    linked = _zone_from_etc_localtime()
    if linked is not None:
        return linked
    offset = utcnow().astimezone().strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"


def local_timezone() -> datetime.tzinfo:
    """Return the machine's local zone, as a ZoneInfo when one can be named.

    A named zone is what makes a recurring schedule survive a DST shift: "07:00
    in Asia/Kolkata" is stable, "07:00 at +05:30" is only true until the offset
    changes.
    """
    name = local_timezone_name()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return utcnow().astimezone().tzinfo or datetime.UTC


def resolve_timezone(name: str | None) -> datetime.tzinfo:
    """Return the zone for IANA *name*, or the machine's local zone when None.

    An unknown name falls back to local rather than raising: a schedule that
    fires at the user's own 07:00 is a better failure than one that never fires.
    """
    if name is None:
        return local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return local_timezone()


def format_timestamp(dt: datetime.datetime | None = None) -> str:
    """Format a datetime as an ISO-8601 string.

    If dt is None, the current UTC time is used.
    """
    if dt is None:
        dt = utcnow()
    return dt.isoformat()


def format_local(value: float | datetime.datetime | None, fmt: str = LOCAL_DISPLAY_FORMAT) -> str:
    """Render an epoch or datetime in local time for display. None renders as "-"."""
    if value is None:
        return "-"
    dt = epoch_to_local(value) if isinstance(value, int | float) else value.astimezone(local_timezone())
    return dt.strftime(fmt)


def parse_local(text: str) -> float:
    """Parse an ISO-8601 timestamp to epoch seconds, reading a naive one as local.

    Accepts the shapes a model or a user actually writes: "2026-07-15T14:30:00Z",
    "2026-07-15T14:30+05:30", "2026-07-15 14:30". Raises ValueError otherwise.
    """
    return to_epoch(datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")))


def _is_known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _zone_from_etc_localtime() -> str | None:
    """Read the IANA name out of /etc/localtime's symlink target (macOS, Linux)."""
    path = Path("/etc/localtime")
    try:
        if not path.is_symlink():
            return None
        target = str(path.readlink())
    except OSError:
        return None
    _, marker, name = target.partition(_ZONEINFO_MARKER)
    if not marker or not _is_known_zone(name):
        return None
    return name
