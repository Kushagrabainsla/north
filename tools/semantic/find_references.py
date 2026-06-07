"""Find all references to a symbol in a file or directory."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class FindReferencesTool(Tool):
    """Find all references to a symbol by grep-like search."""

    name = "find_references"
    description = (
        "Find all references to a symbol (function, class, variable name) in a file or directory. "
        "Uses regex matching; returns line numbers and context."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Symbol name to search for (e.g., 'my_function')",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["symbol", "path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        count = len(data.get("references", []))
        return f"Found {count} references to `{data.get('symbol', '?')}`."

    async def run(self, input: ToolInput) -> ToolOutput:
        symbol = input.params.get("symbol")
        path_str = input.params.get("path")

        if not symbol:
            return ToolOutput(success=False, error="Parameter 'symbol' is required.")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        return await asyncio.to_thread(_find_references_sync, symbol, resolved)


def _find_references_sync(symbol: str, path: Path) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"Path not found: {path}")

    pattern = rf"\b{re.escape(symbol)}\b"
    try:
        compiled_pattern = re.compile(pattern)
    except re.error as exc:
        return ToolOutput(success=False, error=f"Invalid regex pattern: {exc}")

    references = []

    if path.is_file():
        if path.suffix == ".py":
            references.extend(_search_file(path, symbol, compiled_pattern))
    else:
        for py_file in path.rglob("*.py"):
            if ".venv" in py_file.parts or "__pycache__" in py_file.parts:
                continue
            references.extend(_search_file(py_file, symbol, compiled_pattern))

    references.sort(key=lambda r: (r["file"], r["line"]))

    return ToolOutput(
        success=True,
        data={
            "symbol": symbol,
            "search_path": str(path),
            "references": references[:100],
            "total": len(references),
            "capped": len(references) > 100,
        },
    )


def _search_file(file: Path, symbol: str, pattern: re.Pattern) -> list[dict]:
    refs = []
    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return refs

    for line_num, line in enumerate(content.splitlines(), 1):
        if pattern.search(line):
            refs.append(
                {
                    "file": str(file),
                    "line": line_num,
                    "text": line.strip()[:100],
                }
            )

    return refs
