"""Parsing of the duration strings providers use to report retry timing."""

from __future__ import annotations


def parse_duration_seconds(value: str) -> float | None:
    """Parse a protobuf Duration string (``"12s"``, ``"0.5s"``, ``"1500ms"``).

    Returns ``None`` when the value is absent or unparseable, so a malformed
    provider header degrades to "no precise signal" rather than raising. Negative
    durations clamp to zero: a reset in the past means retry now.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("ms"):
            return max(0.0, float(value[:-2]) / 1000.0)
        if value.endswith("s"):
            return max(0.0, float(value[:-1]))
        return max(0.0, float(value))
    except ValueError:
        return None
