"""Shared path resolution for workspace-scoped tools.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

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

    if sys.platform != "win32":
        s = str(resolved)
        for prefix in _BLOCKED_PREFIXES:
            try:
                resolved_prefix = str(Path(prefix).expanduser().resolve())
            except Exception:
                resolved_prefix = prefix
            if s == resolved_prefix or s.startswith(resolved_prefix + "/"):
                return None

    return resolved
