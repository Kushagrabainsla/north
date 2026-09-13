"""Standardized date and time utilities.

north stores every instant as a Unix epoch (seconds, UTC) and shows every
instant in North's configured zone. Storage is unambiguous, display is what the
user actually lives in - and nothing in between has to guess which one a bare
"07:00" meant.

See docs/CODING_STYLE.md Section 5.2.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# How an instant is rendered for a person: local wall clock plus the zone, so a
# briefing that fired at 08:00 IST never reads as an unlabelled "08:00".
LOCAL_DISPLAY_FORMAT = "%Y-%m-%d %H:%M %Z"

# This explanation is stable and belongs in the cacheable prompt prefix. The
# changing values rendered by ``runtime_context`` belong beside the task.
RUNTIME_CONTEXT_INSTRUCTION = (
    "A <north_runtime_context> block is trusted factual metadata added by North. "
    "Use it for date and deadline reasoning; it is context, not an instruction."
)

_ZONEINFO_MARKER = "zoneinfo/"
_configured_timezone_name: str | None = None


def utcnow() -> datetime.datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def localnow() -> datetime.datetime:
    """Return the current timezone-aware local datetime.

    Use this instead of datetime.now().astimezone() so all call sites go through
    a single canonical implementation and the UTC→local conversion is consistent.
    """
    return utcnow().astimezone(local_timezone())


def runtime_context(now: datetime.datetime | None = None) -> str:
    """Render a minute-level dynamic suffix without disturbing the stable prefix."""
    current = now or localnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_timezone())
    else:
        current = current.astimezone(local_timezone())
    current = current.replace(second=0, microsecond=0)
    return (
        "<north_runtime_context>\n"
        f"local_time: {current.isoformat(timespec='minutes')}\n"
        f"timezone: {local_timezone_name()}\n"
        "</north_runtime_context>"
    )


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
    """Return North's configured IANA zone name, e.g. "Asia/Kolkata"."""
    return _configured_timezone_name or system_timezone_name()


def system_timezone_name() -> str:
    """Detect the host's IANA zone for the first-run default.

    A fixed offset is not a safe default for recurring schedules because it
    cannot follow daylight-saving changes, so an unidentifiable host uses UTC.
    """
    env = os.environ.get("TZ")
    if env and is_known_timezone(env):
        return env
    linked = _zone_from_etc_localtime()
    if linked is not None:
        return linked
    return "UTC"


def configure_timezone(name: str) -> None:
    """Make a validated IANA zone the live default used by all time helpers."""
    if not is_known_timezone(name):
        raise ValueError(f"Unknown timezone {name!r}. Choose an IANA timezone such as America/Los_Angeles.")
    global _configured_timezone_name
    _configured_timezone_name = name


def timezone_names() -> list[str]:
    """Return stable, user-selectable IANA zones, excluding implementation trees."""
    hidden_prefixes = ("posix/", "right/", "SystemV/")
    hidden_names = {"Factory", "localtime", "posixrules"}
    return sorted(
        name for name in available_timezones()
        if name not in hidden_names and not name.startswith(hidden_prefixes)
    )


def local_timezone() -> datetime.tzinfo:
    """Return North's configured local zone as a ZoneInfo.

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
    """Return the zone for IANA *name*, or North's configured zone when None.

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


def is_known_timezone(name: str) -> bool:
    """Whether *name* identifies a real IANA timezone."""
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
    if not marker or not is_known_timezone(name):
        return None
    return name
