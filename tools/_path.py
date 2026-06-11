"""Shared path resolution for workspace-scoped tools.

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

# Well-known sensitive system directories blocked when no workspace is set.
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
    return any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in _resolved_blocked_prefixes())


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
    - Without workspace: path is resolved from CWD; blocked from sensitive
      system directories to prevent inadvertent leakage of /etc/passwd etc.

    Returns ``None`` when the path is denied.
    """
    p = Path(path_str).expanduser()

    if workspace:
        root = Path(workspace).resolve()
        candidate = (root / p).resolve()
        if not str(candidate).startswith(str(root) + "/") and candidate != root:
            return None
        return candidate

    # No workspace — resolve relative to CWD, block system paths on POSIX.
    resolved = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()

    if sys.platform != "win32" and _is_blocked_path(str(resolved)):
        return None

    return resolved
