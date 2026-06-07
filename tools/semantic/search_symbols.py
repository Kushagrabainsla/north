"""Search for function and class definitions using AST."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class SearchSymbolsTool(Tool):
    """Find function and class definitions in Python files."""

    name = "search_symbols"
    description = (
        "Search for function and class definitions in a Python file. "
        "Returns exact line numbers and signatures. Works only on Python files."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Python file path to search",
            },
            "type": {
                "type": "string",
                "enum": ["function", "class", "all"],
                "description": "Search for functions, classes, or both (default: all)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        count = len(data.get("symbols", []))
        return f"Found {count} symbols in `{data.get('path', '?')}`."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        if resolved.suffix != ".py":
            return ToolOutput(success=False, error="search_symbols only works on .py files.")

        search_type = input.params.get("type", "all")
        if search_type not in ("function", "class", "all"):
            return ToolOutput(success=False, error="type must be 'function', 'class', or 'all'.")

        return await asyncio.to_thread(_search_symbols_sync, resolved, search_type)


def _search_symbols_sync(path: Path, search_type: str) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except SyntaxError as exc:
        return ToolOutput(success=False, error=f"Syntax error in file: {exc}")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Cannot parse file: {exc}")

    symbols = []
    for node in ast.walk(tree):
        if search_type in ("function", "all") and isinstance(node, ast.FunctionDef):
            args = ", ".join(arg.arg for arg in node.args.args)
            symbols.append(
                {
                    "name": node.name,
                    "type": "function",
                    "line": node.lineno,
                    "signature": f"def {node.name}({args})",
                }
            )
        elif search_type in ("class", "all") and isinstance(node, ast.ClassDef):
            symbols.append(
                {
                    "name": node.name,
                    "type": "class",
                    "line": node.lineno,
                    "signature": f"class {node.name}",
                }
            )

    symbols.sort(key=lambda s: s["line"])

    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "symbols": symbols,
        },
    )
