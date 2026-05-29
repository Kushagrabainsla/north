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
        "Searches the web for up-to-date information. "
        "Use for current events, facts, prices, or anything requiring real-time data."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
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

        max_results = int(input.params.get("max_results", 5))
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
