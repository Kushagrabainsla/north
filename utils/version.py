"""Single source of truth for the running north version.

Reads the installed package metadata (which comes from pyproject.toml), so the
version is bumped in exactly one place. Falls back to "0.0.0+unknown" when the
package is not installed (e.g. running straight from a source checkout).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    NORTH_VERSION: str = version("north")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    NORTH_VERSION = "0.0.0+unknown"
