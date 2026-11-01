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
from tools.registry import LearnedTool, is_learned_tool


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

    served = registry.get("web_search")
    assert isinstance(served, LearnedTool) and served.inner is learned  # served through the gate
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


# A tool north wrote for itself goes through the gate on every call (issue #76, finding M5)


class _CallsItselfHarmless(Tool):
    """What a learned tool can claim about itself: never a change, nothing to describe, no gate."""

    name = "show_keys"
    description = "Show the keys in north's .env."
    is_mutating = False
    parameters_schema = {"type": "object", "properties": {"reveal": {"type": "boolean"}}}

    def mutates(self, params=None) -> bool:
        return False

    async def describe(self, input: ToolInput):
        return None

    async def execute(self, input: ToolInput) -> ToolOutput:
        return await self.run(input)

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={"secret": "the-secret"})


def _learned() -> Tool:
    tool = _CallsItselfHarmless()
    type(tool).__module__ = "north_learned_tool_show_keys"
    return tool


async def test_a_learned_tool_that_calls_itself_harmless_still_asks_on_every_call() -> None:
    """The learned `show_keys` ran with no card and printed north's secret key into chat."""
    from tests.conftest import approvals, rejecting_store

    store = rejecting_store()
    registry = ToolRegistry(approvals=approvals(store=store))
    registry.register(_learned())

    output = await registry.get("show_keys").execute(ToolInput(params={"reveal": True, "task_id": "t1"}))

    assert output.failure_kind == "refused" and "the-secret" not in str(output.data)
    store.add.assert_called_once()
    assert "show_keys" in store.add.call_args.args[0].message


async def test_an_approved_call_of_a_learned_tool_runs_it() -> None:
    from tests.conftest import approvals, approving_store

    registry = ToolRegistry(approvals=approvals(store=approving_store()))
    registry.register(_learned())

    output = await registry.get("show_keys").execute(ToolInput(params={"reveal": False, "task_id": "t1"}))

    assert output.success and output.data == {"secret": "the-secret"}


def test_the_tools_page_still_calls_a_wrapped_learned_tool_learned() -> None:
    registry = ToolRegistry()
    registry.register(_learned())

    assert is_learned_tool(registry.get("show_keys"))
