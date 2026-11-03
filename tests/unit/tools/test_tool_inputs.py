"""A tool call's inputs are checked against what the tool declares, once, for every caller.

In the issue #76 test (W5) the planner's direct calls sent `{"order_id": 4471}` to
an MCP tool declaring a string `order_id`, and once `{"id": 9999}`; a strict
server such as the official MCP SDK rejects both ("Input should be a valid
string"), and the call fell back to an agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from config.approval_mode import ApprovalMode
from mcp.models import McpToolDefinition
from tests.conftest import bind_approvals
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized.mcp_tool import McpTool


class _Records(Tool):
    name = "set_timer"
    description = "Sets a timer."
    parameters_schema = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "minutes": {"type": "integer"},
            "loud": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["label", "minutes"],
    }

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def run(self, input: ToolInput) -> ToolOutput:
        self.received.append(dict(input.params))
        return ToolOutput(success=True, data={})


async def test_a_value_is_converted_only_where_nothing_it_means_changes() -> None:
    tool = _Records()

    out = await tool.execute(ToolInput(params={"label": 4471, "minutes": "10", "loud": "true", "note": None, "x": 1}))

    assert out.success
    assert tool.received == [{"label": "4471", "minutes": 10, "loud": True, "note": None, "x": 1}]


async def test_a_call_that_does_not_fit_is_not_run_and_says_what_the_tool_takes() -> None:
    tool = _Records()

    out = await tool.execute(ToolInput(params={"id": 9999, "minutes": "ten"}))

    assert not out.success and tool.received == []
    assert "missing 'label'" in out.error and "'minutes' must be integer, not 'ten'" in out.error
    assert out.error.endswith("It takes: label, minutes, loud, note.")


async def test_an_mcp_server_gets_the_types_it_declares() -> None:
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=MagicMock(is_error=False, content=[], structured_content=None))
    lookup = McpToolDefinition.model_validate(
        {
            "name": "lookup_order",
            "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
            "annotations": {"readOnlyHint": True},
        }
    )
    tool = bind_approvals(McpTool("orders", client, lookup), ApprovalMode.ASK)

    await tool.execute(ToolInput(params={"order_id": 4471}))
    refused = await tool.execute(ToolInput(params={"id": 9999}))

    client.call_tool.assert_awaited_once_with("lookup_order", {"order_id": "4471"})
    assert not refused.success and "missing 'order_id'" in refused.error
