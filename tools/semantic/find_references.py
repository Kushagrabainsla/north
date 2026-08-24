"""Find textual references to a symbol in a file or directory.

Best-effort textual search, not semantic analysis: it word-boundary-matches
the symbol across source files (reusing the search_files engine - ripgrep when
available, bounded Python fallback otherwise), so it also matches comments and
strings and cannot see aliased imports. Verify behaviour-affecting conclusions
with check_types or the test suite.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from context.lsp_client import LspError, LspUnavailable, server_command_for
from context.lsp_client import find_references as lsp_find_references
from tools._path import find_project_root, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.search_files import SearchOptions, run_search

_MAX_REFERENCES = 100

# Source extensions searched in directory mode - references must be found in
# every language the engineering agents work in, not just Python.
SOURCE_GLOBS: tuple[str, ...] = (
    "*.py",
    "*.pyi",
    "*.ts",
    "*.tsx",
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.go",
    "*.rs",
    "*.java",
    "*.rb",
    "*.c",
    "*.h",
    "*.cpp",
    "*.hpp",
    "*.cs",
    "*.swift",
    "*.kt",
)
_SOURCE_SUFFIXES: frozenset[str] = frozenset(g.lstrip("*") for g in SOURCE_GLOBS)


class FindReferencesTool(Tool):
    """Find references to a symbol by semantic LSP search or word-boundary fallback."""

    name = "find_references"
    description = (
        "Find references to a symbol (function, class, variable name) in a file "
        "or directory, across common source languages (Python, TS/JS, Go, Rust, Java, ...). "
        "Uses language server semantic analysis when available, falling back to fast "
        "text search. Returns line numbers and context."
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
        engine = data.get("engine", "textual")
        return f"Found {count} {engine} references to `{data.get('symbol', '?')}`."

    async def run(self, input: ToolInput) -> ToolOutput:
        symbol = input.params.get("symbol")
        path_str = input.params.get("path")
        workspace = input.params.get("workspace")

        if not symbol:
            return ToolOutput(success=False, error="Parameter 'symbol' is required.")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, workspace)
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")
        if not resolved.exists():
            return ToolOutput(success=False, error=f"Path not found: {resolved}")

        if resolved.is_file() and resolved.suffix not in _SOURCE_SUFFIXES:
            # An explicit error beats a falsely reassuring "0 references".
            return ToolOutput(
                success=False,
                error=(
                    f"find_references does not support {resolved.suffix!r} files. "
                    f"Supported: {', '.join(sorted(_SOURCE_SUFFIXES))}"
                ),
            )

        # Tier 1: Try precise semantic LSP lookup if language server is available
        if resolved.is_file() and server_command_for(resolved.suffix):
            root = Path(workspace).resolve() if workspace else find_project_root(resolved)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            # Locate symbol offset in file
            match_pos = None
            for idx, text_line in enumerate(content.splitlines()):
                m = re.search(r"\b" + re.escape(symbol) + r"\b", text_line)
                if m:
                    match_pos = (idx, m.start())
                    break
            if match_pos is not None:
                try:
                    refs = await asyncio.to_thread(lsp_find_references, root, resolved, match_pos[0], match_pos[1])
                    if refs:
                        references = []
                        for rel_file, r_line, _ in refs[:_MAX_REFERENCES]:
                            p = Path(rel_file)
                            ref_path = (root / p).resolve() if not p.is_absolute() else p
                            text_snippet = ""
                            if ref_path.exists() and ref_path.is_file():
                                try:
                                    snippet_lines = ref_path.read_text(encoding="utf-8", errors="replace").splitlines()
                                    if 0 <= r_line - 1 < len(snippet_lines):
                                        text_snippet = snippet_lines[r_line - 1].strip()[:100]
                                except Exception:
                                    pass
                            references.append({"file": str(ref_path), "line": r_line, "text": text_snippet})
                        references.sort(key=lambda r: (r["file"], r["line"]))
                        return ToolOutput(
                            success=True,
                            data={
                                "symbol": symbol,
                                "search_path": str(resolved),
                                "references": references,
                                "total": len(references),
                                "capped": len(references) >= _MAX_REFERENCES,
                                "engine": "lsp",
                            },
                        )
                except (LspUnavailable, LspError):
                    pass

        # Tier 2: Textual search (ripgrep with Python fallback)
        globs = (f"*{resolved.suffix}",) if resolved.is_file() else SOURCE_GLOBS
        options = SearchOptions(globs=globs, mode="content", context=0, limit=_MAX_REFERENCES)
        result = await run_search(resolved, rf"\b{re.escape(symbol)}\b", options)
        if not result.success:
            return result

        matches = (result.data or {}).get("matches", [])
        references = [
            {"file": _relative_or_abs(m["file"], resolved), "line": m["line"], "text": m["text"].strip()[:100]}
            for m in matches
        ]
        references.sort(key=lambda r: (r["file"], r["line"]))

        return ToolOutput(
            success=True,
            data={
                "symbol": symbol,
                "search_path": str(resolved),
                "references": references,
                "total": len(references),
                "capped": len(references) >= _MAX_REFERENCES,
                "engine": "textual",
            },
        )


def _relative_or_abs(file: str, base: Path) -> str:
    """ripgrep reports paths under the search base; normalize to absolute strings."""
    p = Path(file)
    return str(p if p.is_absolute() else base / p)
