"""Pure scheduling-input normalization shared by CLI command handlers."""

from __future__ import annotations

from collections.abc import Callable


def day_selection(days: str | None, validate: Callable[[list[str] | str], object]) -> list[str] | str | None:
    """Normalize comma-separated days and validate them using the API's reader."""
    if days is None:
        return None
    selection: list[str] | str = (
        [part.strip() for part in days.split(",") if part.strip()] if "," in days else days.strip()
    )
    validate(selection)
    return selection
