"""Tests for ToolRegistry and the canonical TOOL_GRAPH."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.registry import AgentRegistry
from tools import (
    Tool,
    ToolInput,
    ToolNotFoundError,
    ToolOutput,
    ToolRegistry,
)

_AGENTS_DIR = Path(__file__).parents[3] / "agents"
TOOL_GRAPH = AgentRegistry.build_tool_graph(_AGENTS_DIR)


def _make_tool(tool_name: str) -> Tool:
    class _T(Tool):
        name = tool_name
        description = f"stub for {tool_name}"

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True)

    return _T()


# TOOL_GRAPH coverage


def test_tool_graph_has_v1_agents() -> None:
    """The four v1 domain agents must each appear in the graph (README Section 7.1)."""
    assert {"health", "university", "job", "finance"}.issubset(set(TOOL_GRAPH.keys()))


def test_tool_graph_has_cross_domain_tools() -> None:
    """web_search and file tools are provided as universal tools to every agent."""
    universal_dir = Path(__file__).parents[3] / "tools" / "universal"
    universal = {p.stem for p in universal_dir.glob("*.py") if not p.name.startswith("_")}
    assert "web_search" in universal
    assert "read_file" in universal
    assert "write_file" in universal


# ToolRegistry - register / get


def test_register_then_get_round_trips() -> None:
    registry = ToolRegistry()
    tool = _make_tool("web_search")
    registry.register(tool)

    assert registry.get("web_search") is tool


def test_get_unknown_tool_raises_tool_not_found() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nonexistent_tool")


# ToolRegistry - agent-level lookups


def test_tools_for_agent_returns_only_registered_tools_in_graph() -> None:
    registry = ToolRegistry(graph={"health": ["web_search"]})
    registry.register(_make_tool("web_search"))
    # read_file intentionally NOT registered - should be silently skipped

    tools = registry.tools_for_agent("health", auto_reload=False)

    assert {t.name for t in tools} == {"web_search"}


def test_tools_for_agent_returns_empty_for_unknown_agent() -> None:
    registry = ToolRegistry()
    assert registry.tools_for_agent("nonexistent_agent", auto_reload=False) == []


def test_agent_names_matches_graph_keys() -> None:
    registry = ToolRegistry(graph=TOOL_GRAPH)
    assert set(registry.agent_names()) == set(TOOL_GRAPH.keys())


def test_all_tool_names_is_union_across_agents() -> None:
    registry = ToolRegistry(graph=TOOL_GRAPH)
    expected = {name for names in TOOL_GRAPH.values() for name in names}
    assert registry.all_tool_names() == expected


# Custom graph injection


def test_registry_accepts_custom_graph() -> None:
    custom = {"my_agent": ["my_tool"]}
    registry = ToolRegistry(graph=custom)
    registry.register(_make_tool("my_tool"))

    assert {t.name for t in registry.tools_for_agent("my_agent", auto_reload=False)} == {"my_tool"}
    assert registry.agent_names() == ["my_agent"]
