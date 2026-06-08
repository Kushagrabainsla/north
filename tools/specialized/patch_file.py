"""PatchFileTool — replace an exact string in a file.

Analogous to Claude Code's Edit tool: finds `old_string` in the file and
replaces it with `new_string`.  Fails loudly if `old_string` is absent or
appears more than once so the model can never silently corrupt a file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class PatchFileTool(Tool):
    """Replace an exact string in a file. Fails if the string is missing or not unique."""

    name = "patch_file"
    description = (
        "Replace an exact string or one or more SEARCH/REPLACE blocks in a file. "
        "For simple replacements, provide path, old_string, and new_string. "
        "For SEARCH/REPLACE blocks, omit old_string and format new_string as:\n"
        "<<<<<<< SEARCH\n"
        "<exact code to find>\n"
        "=======\n"
        "<replacement code>\n"
        ">>>>>>> REPLACE"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "old_string": {
                "type": "string",
                "description": (
                    "Exact text to find — must appear exactly once in the file."
                    " Optional if using SEARCH/REPLACE blocks in new_string."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text or SEARCH/REPLACE blocks",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path", "new_string"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Patched `{data.get('path', '?')}` successfully."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        old_string = input.params.get("old_string")
        new_string = input.params.get("new_string")

        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")
        if new_string is None:
            return ToolOutput(success=False, error="Parameter 'new_string' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        return await asyncio.to_thread(_patch_sync, resolved, old_string, new_string)


def _patch_sync(path: Path, old_string: str | None, new_string: str) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be patched: {path}")

    import re

    # Check if new_string contains SEARCH/REPLACE blocks
    blocks = re.findall(r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE", new_string, re.DOTALL)

    if blocks:
        # Check all blocks first
        new_content = content
        for search_val, replace_val in blocks:
            count = new_content.count(search_val)
            if count == 0:
                return ToolOutput(
                    success=False,
                    error=f"SEARCH block not found in file:\n{search_val}\nCheck for exact spacing/newlines.",
                )
            if count > 1:
                return ToolOutput(
                    success=False,
                    error=f"SEARCH block is not unique, appears {count} times:\n{search_val}",
                )
            new_content = new_content.replace(search_val, replace_val, 1)

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=str(exc))

        return ToolOutput(
            success=True,
            data={
                "path": str(path),
                "bytes_before": len(content.encode("utf-8")),
                "bytes_after": len(new_content.encode("utf-8")),
                "blocks_applied": len(blocks),
            },
        )

    # Legacy fallback: simple string replacement
    if old_string is None:
        return ToolOutput(
            success=False,
            error="Either old_string must be provided, or new_string must contain SEARCH/REPLACE blocks.",
        )

    count = content.count(old_string)
    if count == 0:
        return ToolOutput(
            success=False,
            error="old_string not found in file. Check for exact whitespace and newlines.",
        )
    if count > 1:
        return ToolOutput(
            success=False,
            error=(
                f"old_string appears {count} times — not unique. "
                "Add more surrounding context to make it match exactly once."
            ),
        )

    new_content = content.replace(old_string, new_string, 1)
    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return ToolOutput(success=False, error=str(exc))

    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "bytes_before": len(content.encode("utf-8")),
            "bytes_after": len(new_content.encode("utf-8")),
            "blocks_applied": 1,
        },
    )
