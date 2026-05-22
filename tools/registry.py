"""Tool registry and the canonical agent→tools graph. See README Section 7.4."""

from __future__ import annotations

from tools.base import Tool
from tools.exceptions import ToolNotFoundError
from tools.implementations.calendar_api import CalendarApiTool
from tools.implementations.canvas_api import CanvasApiTool
from tools.implementations.expense_tracker import ExpenseTrackerTool
from tools.implementations.fitness_tracker import FitnessTrackerTool
from tools.implementations.gmail_api import GmailApiTool
from tools.implementations.linkedin_api import LinkedinApiTool
from tools.implementations.market_data_api import MarketDataApiTool
from tools.implementations.nutrition_api import NutritionApiTool
from tools.implementations.web_search import WebSearchTool

TOOL_GRAPH: dict[str, list[str]] = {
    "health":     ["web_search", "calendar_api", "nutrition_api"],
    "university": ["web_search", "calendar_api", "gmail_api", "canvas_api"],
    "job":        ["web_search", "calendar_api", "gmail_api", "linkedin_api"],
    "finance":    ["web_search", "gmail_api", "market_data_api", "expense_tracker"],
}

# Mapping of tool names to concrete class instances (docs/CODING_STYLE.md Section 6.5)
TOOL_IMPLEMENTATIONS: dict[str, Tool] = {
    "web_search": WebSearchTool(),
    "calendar_api": CalendarApiTool(),
    "gmail_api": GmailApiTool(),
    "canvas_api": CanvasApiTool(),
    "nutrition_api": NutritionApiTool(),
    "market_data_api": MarketDataApiTool(),
    "linkedin_api": LinkedinApiTool(),
    "fitness_tracker": FitnessTrackerTool(),
    "expense_tracker": ExpenseTrackerTool(),
}


class ToolRegistry:
    """In-memory registry of `Tool` instances plus the agent→tools graph.

    Tools are registered by name. `tools_for_agent` returns the Tool
    instances available to an agent according to the graph — unordered.
    Combining with `ConfidenceTracker.scores_for_agent` to load in
    confidence order belongs to the Orchestrator, not here.
    """

    def __init__(self, graph: dict[str, list[str]] | None = None, auto_register: bool = False) -> None:
        self._graph: dict[str, list[str]] = graph if graph is not None else TOOL_GRAPH
        self._tools: dict[str, Tool] = {}
        # Populate default tool implementations if requested
        if auto_register:
            for tool in TOOL_IMPLEMENTATIONS.values():
                self.register(tool)

    def register(self, tool: Tool) -> None:
        """Add `tool` to the registry, keyed on `tool.name`."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Return the registered tool with the given name.

        Raises:
            ToolNotFoundError: if no tool is registered under that name.
        """
        if name not in self._tools:
            raise ToolNotFoundError(f"No tool registered with name: {name}")
        return self._tools[name]

    def tools_for_agent(self, agent: str) -> list[Tool]:
        """Return Tool instances available to `agent` per the graph.

        Tool names in the graph that have no registered implementation
        are skipped (allows partial registries during early bring-up).
        """
        if agent not in self._graph:
            return []
        return [self._tools[name] for name in self._graph[agent] if name in self._tools]

    def agent_names(self) -> list[str]:
        """Names of agents declared in the graph."""
        return list(self._graph.keys())

    def all_tool_names(self) -> set[str]:
        """Every tool name that appears in the graph, across all agents."""
        return {name for names in self._graph.values() for name in names}
