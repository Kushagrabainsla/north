"""Single source of truth for the running north version.

`pyproject.toml` holds the version; nothing else in the codebase may hardcode it.
Read it from here (`NORTH_VERSION`) wherever a version is needed - the API apps,
the CLI, the MCP client handshake.

Resolution order:
  1. the adjacent `pyproject.toml`, when there is one - it is the file that gets
     edited, and installed metadata is frozen at install time, so an editable
     install otherwise keeps reporting the version it was first installed at
  2. installed package metadata - the case for a real (non-editable) install,
     where `pyproject.toml` is not shipped in the wheel
  3. "0.0.0+unknown" when neither is available
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _version_from_pyproject() -> str | None:
    """Read the version straight from pyproject.toml, or None if unreadable."""
    try:
        with _PYPROJECT.open("rb") as handle:
            return tomllib.load(handle).get("project", {}).get("version")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _resolve_version() -> str:
    declared = _version_from_pyproject()
    if declared:
        return declared
    try:
        return version("north")
    except PackageNotFoundError:
        return "0.0.0+unknown"


NORTH_VERSION: str = _resolve_version()
