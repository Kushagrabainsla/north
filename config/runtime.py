"""Runtime handle to the live dependency container.

north builds its dependencies once at startup. To let config changes take
effect without a restart (e.g. north_config set NORTH_OPENCODE_ZEN_API_KEY=...),
we keep a single mutable reference here. app.py populates it during startup;
north_config.set reads it to rebuild the inference router in place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.dependencies import Dependencies

# Populated by app.py at startup. None before the server is fully up.
_runtime: Dependencies | None = None


def set_runtime(deps: Dependencies) -> None:
    """Store the live dependency container."""
    global _runtime
    _runtime = deps


def get_runtime() -> Dependencies | None:
    """Return the live dependency container, or None if not started yet."""
    return _runtime


def clear_runtime() -> None:
    """Drop the reference (used on shutdown)."""
    global _runtime
    _runtime = None
