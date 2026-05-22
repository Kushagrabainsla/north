"""Tests for ToolRegistry and the canonical TOOL_GRAPH."""

from __future__ import annotations

import pytest

from tools import (
    TOOL_GRAPH,
    Tool,
    ToolInput,
    ToolNotFoundError,
    ToolOutput,
    ToolRegistry,
)


def _make_tool(tool_name: str) -> Tool:
    class _T(Tool):
        name = tool_name
        description = f"stub for {tool_name}"

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True)

    return _T()


# TOOL_GRAPH coverage


def test_tool_graph_has_v1_agents() -> None:
    """The four v1 agents must each appear in the graph (README Section 7.1)."""
    assert set(TOOL_GRAPH.keys()) == {"health", "university", "job", "finance"}


def test_tool_graph_has_cross_domain_tools() -> None:
    """web_search, calendar_api, and gmail_api appear in more than one agent."""
    appearances: dict[str, int] = {}
    for tools in TOOL_GRAPH.values():
        for t in tools:
            appearances[t] = appearances.get(t, 0) + 1
    assert appearances["web_search"] >= 3
    assert appearances["calendar_api"] >= 2
    assert appearances["gmail_api"] >= 2


# ToolRegistry — register / get


def test_register_then_get_round_trips() -> None:
    registry = ToolRegistry()
    tool = _make_tool("web_search")
    registry.register(tool)

    assert registry.get("web_search") is tool


def test_get_unknown_tool_raises_tool_not_found() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nonexistent_tool")


# ToolRegistry — agent-level lookups


def test_tools_for_agent_returns_only_registered_tools_in_graph() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("web_search"))
    registry.register(_make_tool("nutrition_api"))
    # calendar_api intentionally NOT registered

    tools = registry.tools_for_agent("health")

    assert {t.name for t in tools} == {"web_search", "nutrition_api"}


def test_tools_for_agent_returns_empty_for_unknown_agent() -> None:
    registry = ToolRegistry()
    assert registry.tools_for_agent("nonexistent_agent") == []


def test_agent_names_matches_graph_keys() -> None:
    registry = ToolRegistry()
    assert set(registry.agent_names()) == set(TOOL_GRAPH.keys())


def test_all_tool_names_is_union_across_agents() -> None:
    registry = ToolRegistry()
    expected = {name for names in TOOL_GRAPH.values() for name in names}
    assert registry.all_tool_names() == expected


# Custom graph injection


def test_registry_accepts_custom_graph() -> None:
    custom = {"my_agent": ["my_tool"]}
    registry = ToolRegistry(graph=custom)
    registry.register(_make_tool("my_tool"))

    assert {t.name for t in registry.tools_for_agent("my_agent")} == {"my_tool"}
    assert registry.agent_names() == ["my_agent"]
