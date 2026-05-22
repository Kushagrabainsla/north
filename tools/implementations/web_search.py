"""Simulated web search tool.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from tools import Tool, ToolInput, ToolOutput


class WebSearchTool(Tool):
    """Simulates searching the web for information."""

    name = "web_search"
    description = "Searches the web for up-to-date information on any topic."

    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute the web search simulation."""
        query = input.params.get("query")
        if not query:
            return ToolOutput(success=False, error="Parameter 'query' is required.")

        data = {
            "query": query,
            "results": [
                {
                    "title": f"Search result for {query}",
                    "snippet": f"This is a simulated search result for the query: '{query}'. It contains useful details.",
                    "url": f"https://example.com/search?q={query}",
                }
            ],
        }
        return ToolOutput(success=True, data=data)
