"""Runtime handle to the live dependency container."""

from __future__ import annotations

from typing import Any

_runtime: Any | None = None


def set_runtime(deps: Any) -> None:
    """Store the live dependency container."""
    global _runtime
    _runtime = deps


def get_runtime() -> Any | None:
    """Return the live dependency container, or ``None`` before startup."""
    return _runtime


def clear_runtime() -> None:
    """Drop the live dependency reference during shutdown."""
    global _runtime
    _runtime = None
