"""ListDirTool — list directory contents in the workspace.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from typing import Any

from tools.base import Tool
from tools.implementations._path import resolve_path
from tools.models import ToolInput, ToolOutput

_MAX_ENTRIES = 200


class ListDirTool(Tool):
    """Lists directory entries sorted dirs-first then files alphabetically."""

    name = "list_dir"
    description = "List directory contents, sorted dirs-first then files alphabetically."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to list (default '.')",
                "default": ".",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
    }

    def format_output(self, data: dict[str, Any]) -> str:
        entries = data.get("entries", [])
        if not entries:
            return "(empty directory)"
        lines = [
            f"{'[dir] ' if e.get('type') == 'dir' else '      '}{e['name']}"
            for e in entries
        ]
        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        resolved = resolve_path(input.params.get("path", "."), input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")
        return await asyncio.to_thread(_list_sync, resolved)


def _list_sync(path: Path) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"Directory not found: {path}")
    if not path.is_dir():
        return ToolOutput(success=False, error=f"Not a directory: {path}")
    try:
        all_entries = list(path.iterdir())
        dirs = sorted([e for e in all_entries if e.is_dir()], key=lambda e: e.name)
        files = sorted([e for e in all_entries if e.is_file()], key=lambda e: e.name)
        entries = [
            {"name": e.name, "type": "dir" if e.is_dir() else "file",
             "size": e.stat().st_size if e.is_file() else 0}
            for e in (dirs + files)[:_MAX_ENTRIES]
        ]
        return ToolOutput(success=True, data={"entries": entries})
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
