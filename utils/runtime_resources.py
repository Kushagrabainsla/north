"""Resolve North's bundled runtime resources.

Resources are shipped under the ``resources`` package.  A checkout fallback is
kept for the remaining legacy resource locations while the repository is being
migrated; installed applications always resolve through the packaged tree.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_RESOURCE_ROOT = _PACKAGE_ROOT / "resources"


def resource_path(relative_path: str | Path) -> Path:
    """Return an existing bundled resource path.

    ``relative_path`` is relative to the new resource root.  For resources
    still living beside their Python owners, the legacy path is accepted as a
    temporary checkout compatibility seam.
    """
    relative = Path(relative_path)
    if relative.is_absolute():
        return relative

    packaged = _RESOURCE_ROOT / relative
    if packaged.exists():
        return packaged

    legacy = _PACKAGE_ROOT / relative
    if legacy.exists():
        return legacy

    raise FileNotFoundError(f"North runtime resource not found: {relative}")


def prompts_dir() -> Path:
    return resource_path("prompts")


def builtin_skills_dir() -> Path:
    return resource_path("builtin-skills")


def policies_dir() -> Path:
    return resource_path("policies")


def web_dist_dir() -> Path:
    return resource_path("web/dist")
