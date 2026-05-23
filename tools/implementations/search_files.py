"""SearchFilesTool — grep-style search across workspace files.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from tools.base import Tool
from tools.implementations._path import resolve_path
from tools.models import ToolInput, ToolOutput

_MAX_MATCHES = 100


class SearchFilesTool(Tool):
    """Searches files recursively for a regex pattern."""

    name = "search_files"
    description = (
        "Search files for a regex pattern. "
        "Params: pattern (str), path (str, default '.'), "
        "workspace (str, optional), file_glob (str, default '*')."
    )

    async def run(self, input: ToolInput) -> ToolOutput:
        pattern = input.params.get("pattern")
        if not pattern:
            return ToolOutput(success=False, error="Parameter 'pattern' is required.")

        resolved = resolve_path(input.params.get("path", "."), input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        file_glob = input.params.get("file_glob", "*")
        return await asyncio.to_thread(_search_sync, resolved, pattern, file_glob)


def _search_sync(base: Path, pattern: str, file_glob: str) -> ToolOutput:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolOutput(success=False, error=f"Invalid regex: {exc}")

    matches: list[dict] = []
    for file in base.rglob(file_glob):
        if not file.is_file() or len(matches) >= _MAX_MATCHES:
            break
        try:
            content = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                matches.append({"file": str(file), "line": i, "text": line})
                if len(matches) >= _MAX_MATCHES:
                    break

    return ToolOutput(success=True, data={"matches": matches})
