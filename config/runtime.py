"""Runtime handle to the live dependency container."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.dependencies import Dependencies

_runtime: Dependencies | None = None


def set_runtime(deps: Dependencies) -> None:
    """Store the live dependency container."""
    global _runtime
    _runtime = deps


def get_runtime() -> Dependencies | None:
    """Return the live dependency container, or ``None`` before startup."""
    return _runtime


def clear_runtime() -> None:
    """Drop the live dependency reference during shutdown."""
    global _runtime
    _runtime = None
