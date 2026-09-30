"""MCP calls ask according to what the server says the tool does (#31).

Every MCP tool used to count as a change. A server's `readOnlyHint` now lets a
read run without asking, and its other hints reach the card and the memory decider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from approval.policy import ActionKind
from mcp.client import McpClient
from mcp.models import McpCallResult, McpToolDefinition
from tests.conftest import bind_approvals, rejecting_store
from tools.models import ToolInput
from tools.specialized.mcp_tool import McpTool


def _tool(**annotations) -> McpTool:
    client = AsyncMock(spec=McpClient)
    client.call_tool.return_value = McpCallResult(content=[{"type": "text", "text": "ok"}])
    definition = McpToolDefinition.model_validate({"name": "search_issues", "annotations": annotations})
    return McpTool(server_name="github", client=client, tool_def=definition)


def test_a_tool_the_server_calls_read_only_is_not_a_change() -> None:
    assert _tool(readOnlyHint=True).mutates({}) is False


def test_without_the_hint_every_call_is_a_change() -> None:
    assert _tool().mutates({}) is True
    assert McpToolDefinition(name="x").annotations.destructive_hint is True, "the spec's default assumes the worst"


async def test_a_read_only_call_runs_without_asking() -> None:
    store = rejecting_store()
    tool = bind_approvals(_tool(readOnlyHint=True), store=store)

    result = await tool.execute(ToolInput(params={"q": "bug"}))

    assert result.success
    store.wait_for_decision.assert_not_awaited()


async def test_a_change_asks_and_the_card_says_what_the_server_said() -> None:
    tool = _tool(title="Search issues", destructiveHint=False, openWorldHint=True)

    request = await tool.describe(ToolInput(params={"q": "bug", "task_id": "t1"}))

    assert request.action.kind is ActionKind.MCP
    assert request.action.command == "github" and request.action.operation == "search_issues"
    assert request.title == "Search issues (github) - Approval Required"
    assert "reaches outside your machine" in request.message
    assert "delete or overwrite" not in request.message
    assert "task_id" not in request.action.args
