"""RenameSymbolTool - semantic project-wide rename via a language server (#4).

Renames a function/class/variable *and every reference to it* across the
workspace using the language server's own rename (a real WorkspaceEdit), not
text substitution - so it never touches a same-named symbol in an unrelated
scope, a comment, or a string. This is the one refactor grep-and-replace cannot
do safely. Python is supported when pyright is installed; for languages without
an available server it fails with a clear message so the coder falls back to
targeted edits. Coder-only (opt-in via tools.yaml) - the reviewer never edits code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from context.lsp_client import LspError, LspUnavailable, server_command_for
from context.lsp_client import rename_symbol as lsp_rename
from tools._path import find_project_root, is_sensitive_path, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class RenameSymbolTool(Tool):
    name = "rename_symbol"
    is_mutating = True
    description = (
        "Semantically rename a symbol (function, class, method, or variable) and every "
        "reference to it across the workspace, using a language server - accurate, scope-aware, "
        "and safe (never touches same-named symbols in other scopes, comments, or strings). "
        "Prefer this over grep-and-replace for renames. Pass the file where the symbol is "
        "defined, its current name, and the new name. Python works when pyright is installed; "
        "otherwise it returns an error and you should do targeted edits. Applies changes to disk "
        "- review them with git diff."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File where the symbol is DEFINED"},
            "symbol": {"type": "string", "description": "Current name of the symbol"},
            "new_name": {"type": "string", "description": "New name for the symbol"},
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path", "symbol", "new_name"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        files = data.get("files_changed", 0)
        edits = data.get("edits", 0)
        changed = data.get("changed", [])
        old, new = data.get("symbol", "?"), data.get("new_name", "?")
        head = f"Renamed `{old}` → `{new}`: {edits} edit(s) across {files} file(s)."
        if changed:
            head += "\n" + "\n".join(f"  - {c}" for c in changed[:20])
        return head

    async def run(self, input: ToolInput) -> ToolOutput:
        symbol = (input.params.get("symbol") or "").strip()
        new_name = (input.params.get("new_name") or "").strip()
        path_str = input.params.get("path")
        if not symbol or not new_name:
            return ToolOutput(success=False, error="Both 'symbol' and 'new_name' are required.")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' (defining file) is required.")
        if not new_name.isidentifier():
            return ToolOutput(success=False, error=f"{new_name!r} is not a valid identifier.")

        workspace = input.params.get("workspace")
        resolved = resolve_path(path_str, workspace)
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")
        if not resolved.is_file():
            return ToolOutput(success=False, error=f"File not found: {resolved}")
        if is_sensitive_path(resolved):
            return ToolOutput(success=False, error="Refusing to operate on a sensitive path.")
        if server_command_for(resolved.suffix) is None:
            return ToolOutput(
                success=False,
                error=(
                    f"No language server available for {resolved.suffix!r} files "
                    "(rename needs one, e.g. pyright for Python). Do a targeted edit instead."
                ),
            )

        root = Path(workspace).resolve() if workspace else find_project_root(resolved)
        try:
            files, edits, changed = await asyncio.to_thread(lsp_rename, root, resolved, symbol, new_name)
        except LspUnavailable as exc:
            return ToolOutput(success=False, error=f"Language server unavailable: {exc}")
        except LspError as exc:
            return ToolOutput(success=False, error=f"Rename failed: {exc}. Do a targeted edit instead.")

        if files == 0:
            return ToolOutput(
                success=False,
                error=f"Could not rename {symbol!r} (no edits produced). Check the symbol name and defining file.",
            )
        return ToolOutput(
            success=True,
            data={
                "symbol": symbol,
                "new_name": new_name,
                "files_changed": files,
                "edits": edits,
                "changed": changed,
            },
        )
