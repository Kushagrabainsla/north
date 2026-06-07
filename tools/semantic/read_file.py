"""Read file contents with optional line range."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class ReadFileTool(Tool):
    """Read file contents optionally within a line range."""

    name = "read_file"
    description = (
        "Read the contents of a file, optionally within a line range. "
        "Returns the file content with line numbers. Useful for understanding code structure before modifying."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "start_line": {
                "type": "integer",
                "description": "Start line number (1-indexed, optional)",
            },
            "end_line": {
                "type": "integer",
                "description": "End line number inclusive (1-indexed, optional)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        path = data.get("path", "?")
        lines_read = data.get("lines_read", 0)
        return f"Read `{path}`: {lines_read} lines."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        start_line = input.params.get("start_line")
        end_line = input.params.get("end_line")

        return await asyncio.to_thread(_read_sync, resolved, start_line, end_line)


def _read_sync(path: Path, start_line: int | None, end_line: int | None) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be read: {path}")

    lines = content.splitlines(keepends=False)
    total_lines = len(lines)

    start = max(0, (start_line or 1) - 1)
    end = min(total_lines, (end_line or total_lines))

    if start >= total_lines:
        return ToolOutput(
            success=False,
            error=f"start_line {start_line} exceeds file length {total_lines}.",
        )

    selected_lines = lines[start:end]
    formatted = "\n".join(f"{i + start + 1}\t{line}" for i, line in enumerate(selected_lines))

    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "lines_read": len(selected_lines),
            "start": start + 1,
            "end": end,
            "total_in_file": total_lines,
            "content": formatted,
        },
    )
