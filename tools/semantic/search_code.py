"""SearchCodeTool - semantic "search by meaning" over the workspace (#2 code RAG).

Unlike grep/search_symbols (which need the exact identifier), this finds the
functions and methods most semantically related to a natural-language query -
e.g. "where do we validate config" returns the config-validation function even
if it's named `_check_settings`. Backed by CodeIndex (embeddings + cosine).
Requires manual registration because it needs the shared CodeIndex.
"""

from __future__ import annotations

from typing import Any

from context.code_index import CodeIndex
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class SearchCodeTool(Tool):
    name = "search_code"
    is_mutating = False
    description = (
        "Semantic code search: find the functions/methods in the workspace most related "
        "to a natural-language query, ranked by meaning (not exact text). Use it to locate "
        "relevant code when you don't know the identifier to grep for - e.g. 'retry with "
        "backoff', 'where config is validated', 'the auth middleware'. Returns file paths, "
        "line ranges, symbol names, and code. For an exact identifier, prefer search_symbols/grep."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of the code you're looking for",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 5)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["query"],
    }

    def __init__(self, code_index: CodeIndex) -> None:
        self._index = code_index

    def format_output(self, data: dict[str, Any]) -> str:
        matches = data.get("matches", [])
        if not matches:
            return "No semantically similar code found (try grep/search_symbols for exact names)."
        lines = [f"Found {len(matches)} relevant code location(s):"]
        for m in matches:
            sym = f" `{m['symbol']}`" if m.get("symbol") else ""
            lines.append(f"- {m['path']}:{m['start_line']}-{m['end_line']}{sym}")
        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        query = (input.params.get("query") or "").strip()
        if not query:
            return ToolOutput(success=False, error="Parameter 'query' is required.")
        workspace = input.params.get("workspace")
        if not workspace:
            return ToolOutput(success=False, error="No workspace in scope for code search.")
        try:
            max_results = int(input.params.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 20))

        results = await self._index.search(workspace, query, max_results)
        matches = [
            {
                "path": path,
                "start_line": start,
                "end_line": end,
                "symbol": symbol,
                "code": code,
            }
            for path, start, end, symbol, code in results
        ]
        return ToolOutput(success=True, data={"matches": matches, "count": len(matches)})
