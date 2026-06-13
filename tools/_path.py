"""Shared path resolution and sensitive-path policy for workspace-scoped tools.

This module is the single fail-closed gate for filesystem access: every tool
that touches a path (read_file, write_file, search_files, glob, list_dir,
check_types, search_symbols, find_references) resolves it through
``resolve_path()``, and every tool that walks a tree prunes via
``PRUNED_DIRS``/``SENSITIVE_DIR_NAMES``. The sensitive-path blocklist applies
in *all* branches — with or without a workspace — so setting a broad workspace
(e.g. $HOME) can never re-open access to ~/.ssh, ~/.north, /etc, etc.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

# Directories never worth walking for a coding task. Shared by the
# file-walking tools (search_files, glob) so the exclusion list cannot drift.
PRUNED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache", "build", "dist"}
)

# Home-relative directory names that hold credentials/secrets. File-walking
# tools must skip these even when the walk root (e.g. a $HOME workspace)
# legitimately contains them.
SENSITIVE_DIR_NAMES: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".config", ".north"})

# Well-known sensitive directories blocked regardless of workspace.
_BLOCKED_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var/run",
    "/private/etc",  # macOS
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config",
    "~/.north",  # north's own home: secret.key, .env, ledger DBs
)

# Filenames whose contents are secrets wherever they live.
_BLOCKED_FILENAMES: frozenset[str] = frozenset({"secret.key"})

# Marker files that identify a project root, checked from a file upward.
_PROJECT_ROOT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "go.mod",
    "package.json",
    "Cargo.toml",
)


@functools.lru_cache(maxsize=1)
def _resolved_blocked_prefixes() -> tuple[str, ...]:
    """Expand and resolve _BLOCKED_PREFIXES once; falls back to the raw prefix."""
    resolved: list[str] = []
    for prefix in _BLOCKED_PREFIXES:
        try:
            resolved.append(str(Path(prefix).expanduser().resolve()))
        except Exception:
            resolved.append(prefix)
    return tuple(resolved)


def _is_blocked_path(resolved: str) -> bool:
    """True when *resolved* (an absolute path string) sits under a blocked prefix."""
    if Path(resolved).name in _BLOCKED_FILENAMES:
        return True
    return any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in _resolved_blocked_prefixes())


def is_sensitive_path(path: Path) -> bool:
    """True when *path* resolves into a blocked sensitive directory or filename.

    Used by file-walking tools to prune sensitive subtrees that live under an
    otherwise-allowed walk root. Matches both the absolute blocklist and the
    name-based credential dirs (.ssh, .aws, ...) so the policy is identical
    whether a tree is walked or a file inside it is targeted directly.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return True  # unresolvable — treat as sensitive (fail closed)
    if any(part in SENSITIVE_DIR_NAMES for part in resolved.parts):
        return True
    return _is_blocked_path(str(resolved))


def references_sensitive_path(command: str) -> bool:
    """True when any path-like token in *command* points into a blocked directory.

    Shared by tools that pass raw command strings to a shell (e.g. BashTool's
    instant-safe fast path) and therefore never go through resolve_path().
    Only absolute and ~-prefixed tokens are checked — relative paths stay
    inside the caller's cwd and are covered by the workspace rules.
    """
    for token in command.split():
        candidate = token.strip("'\"")
        if not candidate.startswith(("/", "~")):
            continue
        try:
            resolved = str(Path(candidate).expanduser().resolve())
        except (OSError, ValueError):
            return True  # unresolvable path-like token — treat as sensitive
        if _is_blocked_path(resolved):
            return True
    return False


def resolve_path(path_str: str, workspace: str | None) -> Path | None:
    """Resolve *path_str*, optionally scoped to *workspace*.

    - With workspace: resolved path must stay inside the workspace root.
    - Without workspace: path is resolved from CWD.
    - In *both* cases the sensitive-path blocklist applies, so a workspace of
      $HOME (or any broad root) never grants access to ~/.ssh, ~/.north, /etc…

    Returns ``None`` when the path is denied.
    """
    p = Path(path_str).expanduser()

    if workspace:
        root = Path(workspace).resolve()
        candidate = (root / p).resolve()
        if not str(candidate).startswith(str(root) + "/") and candidate != root:
            return None
    else:
        candidate = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()

    if sys.platform != "win32" and _is_blocked_path(str(candidate)):
        return None

    return candidate


def find_project_root(path: Path, markers: tuple[str, ...] = _PROJECT_ROOT_MARKERS) -> Path:
    """Return the nearest enclosing directory containing one of *markers*.

    Walks upward from *path* (its parent when *path* is a file). Falls back to
    the file's own directory when no marker is found. Shared by check_types and
    the semantic tools so toolchain/config discovery is consistent.
    """
    start = path if path.is_dir() else path.parent
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory
    return start
