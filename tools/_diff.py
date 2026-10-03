"""A bounded unified diff, for a card that shows what a file write would change."""

from __future__ import annotations

import difflib
from pathlib import Path

_MAX_DIFF_CHARS = 8_000


def unified_diff(path: Path, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
    )
    diff = "".join(lines)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + f"\n[…{len(diff) - _MAX_DIFF_CHARS} chars of diff truncated]"
    return diff
