"""List directory contents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class ListDirTool(Tool):
    """List files and directories at a given path."""

    name = "list_dir"
    description = (
        "List files and directories at a path. Returns file types, sizes, and modification times. "
        "Useful for exploring project structure without spawning shell commands."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to list",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        path = data.get("path", "?")
        count = len(data.get("entries", []))
        return f"Listed `{path}`: {count} entries."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        return await asyncio.to_thread(_list_dir_sync, resolved)


def _list_dir_sync(path: Path) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"Path not found: {path}")
    if not path.is_dir():
        return ToolOutput(success=False, error=f"Not a directory: {path}")

    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except OSError as exc:
        return ToolOutput(success=False, error=f"Cannot read directory: {exc}")

    result = []
    for entry in entries:
        stat = entry.stat()
        result.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size_bytes": stat.st_size if entry.is_file() else None,
                "modified": stat.st_mtime,
            }
        )

    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "entries": result,
        },
    )
