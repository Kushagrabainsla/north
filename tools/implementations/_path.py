"""Shared path resolution for workspace-scoped tools.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from pathlib import Path


def resolve_path(path_str: str, workspace: str | None) -> Path | None:
    """Resolve path_str, optionally scoped to workspace.

    Returns None if the resolved path escapes the workspace root.
    """
    p = Path(path_str)
    if workspace:
        root = Path(workspace).resolve()
        candidate = (root / p).resolve()
        # Reject path traversal outside workspace
        if not str(candidate).startswith(str(root) + "/") and candidate != root:
            return None
        return candidate
    return p.resolve()
