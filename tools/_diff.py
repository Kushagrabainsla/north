"""A bounded unified diff, for a card that shows what a file write would change."""

from __future__ import annotations

import difflib
from pathlib import Path

_MAX_DIFF_CHARS = 8_000


# git's marker for a last line with no newline after it.
_NO_NEWLINE = "\\ No newline at end of file\n"


def unified_diff(path: Path, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
    )
    # A last line with no newline ran into the next one: "-meeting moved to friday+ALL MEETINGS CANCELLED".
    diff = "".join(line if line.endswith("\n") else f"{line}\n{_NO_NEWLINE}" for line in lines)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + f"\n[…{len(diff) - _MAX_DIFF_CHARS} chars of diff truncated]"
    return diff
