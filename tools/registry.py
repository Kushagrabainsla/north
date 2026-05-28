"""Tool registry and the canonical agent→tools graph. See README Section 7.4."""

from __future__ import annotations

from tools.base import Tool
from tools.exceptions import ToolNotFoundError
from tools.implementations.bash import BashTool
from tools.implementations.fetch_url import FetchUrlTool
from tools.implementations.git_tool import GitTool
from tools.implementations.list_dir import ListDirTool
from tools.implementations.patch_file import PatchFileTool
from tools.implementations.read_file import ReadFileTool
from tools.implementations.search_files import SearchFilesTool
from tools.implementations.web_search import WebSearchTool
from tools.implementations.write_file import WriteFileTool

_FILE_TOOLS = ["read_file", "write_file", "list_dir", "search_files"]
_CODE_TOOLS = _FILE_TOOLS + ["bash", "web_search", "fetch_url"]

TOOL_GRAPH: dict[str, list[str]] = {
    # Domain agents: web search + URL fetch + file I/O + scheduling.
    "health":     ["web_search", "fetch_url", "schedule_task"] + _FILE_TOOLS,
    "university": ["web_search", "fetch_url", "schedule_task"] + _FILE_TOOLS,
    "job":        ["web_search", "fetch_url", "schedule_task"] + _FILE_TOOLS,
    "finance":    ["web_search", "fetch_url", "schedule_task"] + _FILE_TOOLS,
    # General: full code tools + scheduling + URL fetch.
    "general":    _CODE_TOOLS + ["schedule_task"],
    # Code: full code tools + precise patch editing + git.
    "code":       _CODE_TOOLS + ["patch_file", "git"],
}

TOOL_IMPLEMENTATIONS: dict[str, Tool] = {
    "web_search":   WebSearchTool(),
    "fetch_url":    FetchUrlTool(),
    "read_file":    ReadFileTool(),
    "write_file":   WriteFileTool(),
    "list_dir":     ListDirTool(),
    "search_files": SearchFilesTool(),
    "bash":         BashTool(),
    "patch_file":   PatchFileTool(),
    "git":          GitTool(),
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
