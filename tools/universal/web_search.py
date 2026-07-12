"""Real web search via DuckDuckGo (no API key required).

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tools import Tool, ToolInput, ToolOutput


class WebSearchTool(Tool):
    """Searches the web using DuckDuckGo and returns real results."""

    name = "web_search"
    description = (
        "Search the web and get back a ranked list of results (title, snippet, and URL). "
        "Use it for current events, real-time facts, prices, or anything outside your "
        "training data or the local workspace. Phrase the query as plain natural-language "
        "keywords; do NOT use operators like site:, quotes, or AND/OR, which many search "
        "backends ignore. Results are short snippets only - to read a page in full, follow "
        "up with fetch_url on its URL. To find code or files in the user's own project, use "
        "search_files, search_code, or glob instead."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Plain-language search keywords, e.g. 'python-kasa energy monitoring'",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (1–10)",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        results = data.get("results", [])
        if not results:
            return "No results."
        lines = []
        for r in results:
            lines.append(f"**{r.get('title', '')}**")
            if r.get("snippet"):
                lines.append(r["snippet"])
            if r.get("url"):
                lines.append(r["url"])
            lines.append("")
        return "\n".join(lines).strip()

    async def run(self, input: ToolInput) -> ToolOutput:
        query = input.params.get("query")
        if not query:
            return ToolOutput(success=False, error="Parameter 'query' is required.")

        try:
            max_results = int(input.params.get("max_results", 5))
        except (ValueError, TypeError):
            max_results = 5
        max_results = min(max(1, max_results), 10)

        try:
            results = await asyncio.to_thread(self._search, query, max_results)
            return ToolOutput(success=True, data={"query": query, "results": results})
        except Exception as exc:
            return ToolOutput(success=False, error=f"Search failed: {exc}")

    def _search(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))

        return [
            {
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "url": r.get("href", ""),
            }
            for r in raw
        ]
