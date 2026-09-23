"""Tests for the global ToolRegistry."""

from __future__ import annotations

import pytest

from tools import (
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


# ToolRegistry - register / get


def test_register_then_get_round_trips() -> None:
    registry = ToolRegistry()
    tool = _make_tool("web_search")
    registry.register(tool)

    assert registry.get("web_search") is tool


def test_remove_learned_override_restores_builtin_fallback() -> None:
    registry = ToolRegistry()
    builtin = _make_tool("web_search")
    learned = _make_tool("web_search")
    type(learned).__module__ = "north_learned_tool_web_search"
    registry.register(builtin)
    registry.register(learned)

    assert registry.get("web_search") is learned
    assert registry.remove("web_search") is True
    assert registry.get("web_search") is builtin


def test_get_unknown_tool_raises_tool_not_found() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nonexistent_tool")


# ToolRegistry - global eligibility


def test_available_tools_returns_every_registered_tool() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("web_search"))
    registry.register(_make_tool("bash"))

    tools = registry.available_tools(auto_reload=False)

    assert {t.name for t in tools} == {"web_search", "bash"}


def test_all_tool_names_matches_global_catalog() -> None:
    registry = ToolRegistry()
    registry.register(_make_tool("my_tool"))
    registry.register(_make_tool("other_tool"))

    assert registry.all_tool_names() == {"my_tool", "other_tool"}
    assert {t.name for t in registry.available_tools(auto_reload=False)} == {
        "my_tool",
        "other_tool",
    }
