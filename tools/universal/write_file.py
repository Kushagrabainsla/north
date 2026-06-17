"""WriteFileTool - write or overwrite a file in the workspace.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class WriteFileTool(Tool):
    """Writes content to a file, creating parent directories as needed."""

    name = "write_file"
    is_mutating = True
    description = "Write or overwrite a file with the given content (creates parent dirs as needed)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination file path"},
            "content": {"type": "string", "description": "Text content to write"},
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path", "content"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Created `{data.get('path', '?')}` ({data.get('bytes_written', 0)} bytes written)."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        content = input.params.get("content")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")
        if content is None:
            return ToolOutput(success=False, error="Parameter 'content' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        return await asyncio.to_thread(_write_sync, resolved, content)


def _write_sync(path: Path, content: str) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolOutput(
            success=True,
            data={"path": str(path), "bytes_written": len(content.encode("utf-8"))},
        )
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
