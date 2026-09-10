"""Path policy for the cockpit's artifact library.

Pure functions only: HTTP status mapping stays with the routes, so the rules
about which files may ever be published can be read and tested on their own.
"""

from __future__ import annotations

import base64
from pathlib import Path

from tools._path import DB_SUFFIXES

# Directories the artifact library may read. ``tasks`` holds the engineering
# pipeline's handoff output (research notes, specs, QA reports).
ARTIFACT_ROOT_NAMES = ("news", "notes", "wellness", "tasks")


def allowed_output_roots(home: Path) -> list[Path]:
    """Return every directory the artifact library may read from."""
    return [home / name for name in ARTIFACT_ROOT_NAMES]


def is_readable_artifact(path: Path) -> bool:
    """Return whether *path* is a publishable file rather than north's own state.

    ``tasks.db`` and its WAL/SHM siblings live beside handoff directories, so the
    same suffix list the tool sandbox blocks on is applied here.
    """
    return path.is_file() and not path.name.endswith(DB_SUFFIXES)


def artifact_task_id(path: Path, home: Path) -> str:
    """Return the task a handoff artifact belongs to, or "" for personal output."""
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        return ""
    return parts[1] if len(parts) > 2 and parts[0] == "tasks" else ""


def encode_artifact_id(path: Path, home: Path) -> str:
    """Encode a home-relative artifact path as an opaque identifier."""
    relative = str(path.relative_to(home))
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


def resolve_artifact(artifact_id: str, home: Path) -> Path | None:
    """Resolve an identifier to a readable artifact, or ``None`` when disallowed.

    ``None`` covers every rejection - undecodable identifiers, paths escaping the
    permitted roots, and non-publishable files - so callers cannot accidentally
    distinguish them and leak whether a path exists.
    """
    try:
        padded = artifact_id + "=" * (-len(artifact_id) % 4)
        relative = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        return None
    resolved_home = home.resolve()
    path = (resolved_home / relative).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in allowed_output_roots(resolved_home)):
        return None
    if not is_readable_artifact(path):
        return None
    return path
