"""Tests for the Tool ABC hierarchy (Tool, AuthenticatedTool, CacheableTool)."""

from __future__ import annotations

import pytest

from tools import AuthenticatedTool, CacheableTool, Tool, ToolInput, ToolOutput


class _NoopTool(Tool):
    name = "noop"
    description = "Returns the input back as output for testing."

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data=input.params)


def test_tool_cannot_be_instantiated_without_run() -> None:
    class _Incomplete(Tool):
        name = "broken"
        description = "no run() method"

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


def test_authenticated_tool_requires_validate_credentials() -> None:
    class _IncompleteAuth(AuthenticatedTool):
        name = "broken_auth"
        description = "missing validate_credentials"

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True)

    with pytest.raises(TypeError):
        _IncompleteAuth()  # type: ignore[abstract]


def test_cacheable_tool_requires_get_and_set_cached() -> None:
    class _IncompleteCache(CacheableTool):
        name = "broken_cache"
        description = "missing cache methods"

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True)

    with pytest.raises(TypeError):
        _IncompleteCache()  # type: ignore[abstract]


async def test_concrete_tool_runs_and_returns_output() -> None:
    tool = _NoopTool()
    result = await tool.run(ToolInput(params={"x": 1}))
    assert result.success is True
    assert result.data == {"x": 1}
