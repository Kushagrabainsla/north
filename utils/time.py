"""Standardized date and time utilities.

See docs/CODING_STYLE.md Section 5.2.
"""

from __future__ import annotations

import datetime


def utcnow() -> datetime.datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def localnow() -> datetime.datetime:
    """Return the current timezone-aware local datetime.

    Use this instead of datetime.now().astimezone() so all call sites go through
    a single canonical implementation and the UTC→local conversion is consistent.
    """
    return utcnow().astimezone()


def format_timestamp(dt: datetime.datetime | None = None) -> str:
    """Format a datetime as an ISO-8601 string.

    If dt is None, the current UTC time is used.
    """
    if dt is None:
        dt = utcnow()
    return dt.isoformat()
