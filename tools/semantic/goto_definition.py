"""GotoDefinitionTool - semantic symbol definition navigation via language server."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from context.lsp_client import LspError, LspUnavailable, server_command_for
from context.lsp_client import goto_definition as lsp_goto_definition
from tools._path import find_project_root, is_sensitive_path, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class GotoDefinitionTool(Tool):
    """Jump to the definition of a symbol across the workspace using a language server."""

    name = "goto_definition"
    description = (
        "Jump directly to the definition/declaration of a symbol (function, class, variable, "
        "or imported module) using the language server. Pass the file path and either line+character "
        "or symbol name. Returns the defining file, line, and column."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File where the symbol is referenced or defined",
            },
            "symbol": {
                "type": "string",
                "description": "Symbol name to jump to definition for (optional if line/column provided)",
            },
            "line": {
                "type": "integer",
                "description": "1-based line number of the symbol reference (optional if symbol provided)",
            },
            "character": {
                "type": "integer",
                "description": "1-based column/character offset (optional, defaults to 1)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        definitions = data.get("definitions", [])
        if not definitions:
            return "No definition found."
        lines = [f"Found {len(definitions)} definition(s):"]
        for d in definitions:
            lines.append(f"  - {d['file']}:{d['line']}:{d['character']}")
        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        symbol = (input.params.get("symbol") or "").strip()
        line = input.params.get("line")
        char = input.params.get("character", 1)

        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        workspace = input.params.get("workspace")
        resolved = resolve_path(path_str, workspace)
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")
        if not resolved.is_file():
            return ToolOutput(success=False, error=f"File not found: {resolved}", failure_kind="not_found")
        if is_sensitive_path(resolved):
            return ToolOutput(success=False, error="Refusing to operate on a sensitive path.")

        if not symbol and line is None:
            return ToolOutput(success=False, error="Either 'symbol' or 'line' must be provided.")

        if server_command_for(resolved.suffix) is None:
            return ToolOutput(
                success=False,
                error=f"No language server available for {resolved.suffix!r} files.",
            )

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()

        # Resolve 0-based (line_0, char_0) coordinates
        line_0 = 0
        char_0 = 0
        if line is not None:
            line_0 = max(0, int(line) - 1)
            char_0 = max(0, int(char) - 1)
        elif symbol:
            # Find symbol location in text
            found = False
            for idx, text_line in enumerate(lines):
                m = re.search(r"\b" + re.escape(symbol) + r"\b", text_line)
                if m:
                    line_0 = idx
                    char_0 = m.start()
                    found = True
                    break
            if not found:
                return ToolOutput(success=False, error=f"Symbol {symbol!r} not found in {resolved.name}")
        else:
            return ToolOutput(success=False, error="Either 'symbol' or 'line' must be provided.")

        root = Path(workspace).resolve() if workspace else find_project_root(resolved)
        try:
            defs = await asyncio.to_thread(lsp_goto_definition, root, resolved, line_0, char_0)
        except LspUnavailable as exc:
            return ToolOutput(success=False, error=f"Language server unavailable: {exc}")
        except LspError as exc:
            return ToolOutput(success=False, error=f"Goto definition failed: {exc}")

        definitions = [{"file": d[0], "line": d[1], "character": d[2]} for d in defs]
        return ToolOutput(
            success=True,
            data={
                "path": str(resolved),
                "symbol": symbol,
                "definitions": definitions,
                "count": len(definitions),
            },
        )
