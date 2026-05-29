"""ReadFileTool — read a workspace file as text.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.implementations._path import resolve_path
from tools.models import ToolInput, ToolOutput

_MAX_LINES = 10_000


class ReadFileTool(Tool):
    """Reads a file and returns its text content."""

    name = "read_file"
    description = "Read a file's full text contents."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("content", "(empty)"))

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        return await asyncio.to_thread(_read_sync, resolved)


def _read_sync(path: Path) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be read as text: {path}")

    lines = content.splitlines()
    truncated = len(lines) > _MAX_LINES
    if truncated:
        content = "\n".join(lines[:_MAX_LINES])

    return ToolOutput(
        success=True,
        data={"content": content, "lines": min(len(lines), _MAX_LINES), "truncated": truncated},
    )
