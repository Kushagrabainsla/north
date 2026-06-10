"""ReadFileTool — read a workspace file as text, optionally a line range.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_MAX_LINES = 10_000


class ReadFileTool(Tool):
    """Reads a file and returns its text content, optionally a line range."""

    name = "read_file"
    description = (
        "Read a file's text contents. Pass start_line and/or end_line (1-based, "
        "inclusive) to read only a slice of a large file instead of the whole thing."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "start_line": {
                "type": "integer",
                "description": "First line to read, 1-based inclusive (optional)",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to read, 1-based inclusive (optional)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        content = data.get("content", "")
        if not content:
            return "(empty)"
        start = data.get("start_line", 1)
        numbered = "\n".join(
            f"{i:>6}\t{line}" for i, line in enumerate(content.splitlines(), start)
        )
        if data.get("truncated"):
            numbered += f"\n… (truncated at {_MAX_LINES} lines)"
        return numbered

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        start_line = _coerce_line(input.params.get("start_line"))
        end_line = _coerce_line(input.params.get("end_line"))
        return await asyncio.to_thread(_read_sync, resolved, start_line, end_line)


def _coerce_line(value: Any) -> int | None:
    """Return a positive 1-based line number, or None when unset/invalid."""
    if value is None:
        return None
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None


def _read_sync(path: Path, start_line: int | None, end_line: int | None) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be read as text: {path}")

    lines = content.splitlines()
    total = len(lines)

    if start_line is None and end_line is None:
        truncated = total > _MAX_LINES
        body = lines[:_MAX_LINES] if truncated else lines
        return ToolOutput(
            success=True,
            data={
                "content": "\n".join(body),
                "start_line": 1,
                "end_line": min(total, _MAX_LINES),
                "lines": len(body),
                "truncated": truncated,
            },
        )

    start = start_line or 1
    end = end_line or total
    if start > total:
        return ToolOutput(
            success=False,
            error=f"start_line {start} is past end of file ({total} lines).",
        )
    end = min(end, total, start + _MAX_LINES - 1)
    body = lines[start - 1 : end]
    return ToolOutput(
        success=True,
        data={
            "content": "\n".join(body),
            "start_line": start,
            "end_line": end,
            "lines": len(body),
            "truncated": end < total,
        },
    )
