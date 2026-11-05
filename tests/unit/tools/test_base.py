"""Tests for the Tool ABC."""

from __future__ import annotations

import pytest

from tools import Tool, ToolInput, ToolOutput


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


async def test_concrete_tool_runs_and_returns_output() -> None:
    tool = _NoopTool()
    result = await tool.run(ToolInput(params={"x": 1}))
    assert result.success is True
    assert result.data == {"x": 1}
