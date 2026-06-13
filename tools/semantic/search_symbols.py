"""Search for function and class definitions.

Python files are parsed with the AST (reliable for def/async def/class).
TypeScript/JavaScript and Go use best-effort regex heuristics: they can match
text in comments/strings and miss decorated/overloaded or unusually formatted
declarations. Treat results as a navigation aid, not authoritative semantic
analysis — verify with check_types or the build.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class SearchSymbolsTool(Tool):
    """Find function and class definitions in Python, TypeScript/JavaScript, and Go files."""

    name = "search_symbols"
    description = (
        "Find function and class definitions in a code file, with line numbers and signatures. "
        "Python uses real AST parsing (incl. async def); TypeScript/JavaScript and Go use "
        "best-effort regex heuristics that may miss unusual declarations or match comments. "
        "Use it to navigate, then verify changes with check_types or tests. "
        "Supports .py, .ts, .tsx, .js, .jsx, and .go files."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to search",
            },
            "type": {
                "type": "string",
                "enum": ["function", "class", "all"],
                "description": "Search for functions/methods, classes/types, or both (default: all)",
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

        supported_suffixes = (".py", ".js", ".jsx", ".ts", ".tsx", ".go")
        if resolved.suffix not in supported_suffixes:
            return ToolOutput(
                success=False,
                error=(
                    f"search_symbols does not support {resolved.suffix} files."
                    f" Supported: {', '.join(supported_suffixes)}"
                ),
            )

        search_type = input.params.get("type", "all")
        if search_type not in ("function", "class", "all"):
            return ToolOutput(success=False, error="type must be 'function', 'class', or 'all'.")

        return await asyncio.to_thread(_search_symbols_dispatch, resolved, search_type)


def _search_symbols_dispatch(path: Path, search_type: str) -> ToolOutput:
    if not path.exists():
        return ToolOutput(success=False, error=f"File not found: {path}")
    if not path.is_file():
        return ToolOutput(success=False, error=f"Not a file: {path}")

    suffix = path.suffix
    if suffix == ".py":
        return _search_python_symbols(path, search_type)
    elif suffix in (".js", ".jsx", ".ts", ".tsx"):
        return _search_js_ts_symbols(path, search_type)
    elif suffix == ".go":
        return _search_go_symbols(path, search_type)
    else:
        return ToolOutput(success=False, error=f"Unsupported suffix: {suffix}")


def _search_python_symbols(path: Path, search_type: str) -> ToolOutput:
    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except SyntaxError as exc:
        return ToolOutput(success=False, error=f"Syntax error in Python file: {exc}")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Cannot parse Python file: {exc}")

    symbols = []
    for node in ast.walk(tree):
        if search_type in ("function", "all") and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(arg.arg for arg in node.args.args)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            symbols.append(
                {
                    "name": node.name,
                    "type": "function",
                    "line": node.lineno,
                    "signature": f"{prefix} {node.name}({args})",
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
    return ToolOutput(success=True, data={"path": str(path), "symbols": symbols})


def _search_js_ts_symbols(path: Path, search_type: str) -> ToolOutput:
    import re

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Cannot read file: {exc}")

    lines = content.splitlines()
    symbols = []

    # Regex patterns
    class_pattern = re.compile(r"\bclass\s+(\w+)(?:\s+extends\s+\w+)?\b")
    func_pattern1 = re.compile(r"\b(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)")
    func_pattern2 = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:\(([^)]*)\)|(\w+))\s*=>")
    method_pattern = re.compile(r"^\s+(?:async\s+)?(\w+)\s*\(([^)]*)\)\s*\{")

    for idx, line in enumerate(lines, 1):
        # Class check
        if search_type in ("class", "all") and (m := class_pattern.search(line)):
            symbols.append({"name": m.group(1), "type": "class", "line": idx, "signature": f"class {m.group(1)}"})
            continue

        # Function check
        if search_type in ("function", "all"):
            if m := func_pattern1.search(line):
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "function",
                        "line": idx,
                        "signature": f"function {m.group(1)}({m.group(2) or ''})",
                    }
                )
            elif m := func_pattern2.search(line):
                args = m.group(2) or m.group(3) or ""
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "function",
                        "line": idx,
                        "signature": f"const {m.group(1)} = ({args}) =>",
                    }
                )
            elif m := method_pattern.search(line):
                # Class method heuristic (must start with leading whitespace)
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "function",
                        "line": idx,
                        "signature": f"method {m.group(1)}({m.group(2) or ''})",
                    }
                )

    symbols.sort(key=lambda s: s["line"])
    return ToolOutput(success=True, data={"path": str(path), "symbols": symbols})


def _search_go_symbols(path: Path, search_type: str) -> ToolOutput:
    import re

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Cannot read file: {exc}")

    lines = content.splitlines()
    symbols = []

    type_pattern = re.compile(r"\btype\s+(\w+)\s+(struct|interface)\b")
    func_pattern = re.compile(r"\bfunc\s+(?:\(([^)]+)\)\s+)?(\w+)\s*\(([^)]*)\)")

    for idx, line in enumerate(lines, 1):
        if search_type in ("class", "all") and (m := type_pattern.search(line)):
            symbols.append(
                {"name": m.group(1), "type": "class", "line": idx, "signature": f"type {m.group(1)} {m.group(2)}"}
            )
            continue

        if search_type in ("function", "all") and (m := func_pattern.search(line)):
            recv = f"({m.group(1)}) " if m.group(1) else ""
            symbols.append(
                {
                    "name": m.group(2),
                    "type": "function",
                    "line": idx,
                    "signature": f"func {recv}{m.group(2)}({m.group(3) or ''})",
                }
            )

    symbols.sort(key=lambda s: s["line"])
    return ToolOutput(success=True, data={"path": str(path), "symbols": symbols})
