"""MCP calls ask according to what the server says the tool does (#31).

Every MCP tool used to count as a change. A server's `readOnlyHint` now lets a
read run without asking, and its other hints reach the card and the memory decider.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

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


async def test_north_context_stays_out_of_strict_mcp_arguments_and_approval_descriptions():
    client = AsyncMock(spec=McpClient)

    async def strict_search(name, arguments):
        assert name == "search_gmail_messages"
        assert arguments == {"query": "from:jobalerts-noreply@linkedin.com"}
        return McpCallResult(content=[{"type": "text", "text": "Message ID: synthetic-alert"}])

    client.call_tool.side_effect = strict_search
    definition = McpToolDefinition.model_validate(
        {
            "name": "search_gmail_messages",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        }
    )
    tool = McpTool("gmail", client, definition)
    input = ToolInput(
        params={"query": "from:jobalerts-noreply@linkedin.com", "workspace": "/tmp/work", "task_id": "t1"}
    )
    request = await tool.describe(input)
    output = await tool.execute(input)
    assert output.success, output.error
    assert json.loads(request.action.args) == {"query": input.params["query"]}
    assert input.params["workspace"] == "/tmp/work" and input.params["task_id"] == "t1"


@pytest.mark.parametrize("declared", ["workspace", "task_id"])
async def test_context_named_arguments_explicitly_declared_by_mcp_are_preserved(declared):
    client = AsyncMock(spec=McpClient)
    client.call_tool.return_value = McpCallResult(content=[{"type": "text", "text": "ok"}])
    definition = McpToolDefinition.model_validate(
        {
            "name": "inspect_job",
            "inputSchema": {"type": "object", "properties": {declared: {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        }
    )
    tool = McpTool("example", client, definition)
    input = ToolInput(params={"workspace": "external-space", "task_id": "external-job"})
    request = await tool.describe(input)
    output = await tool.execute(input)
    assert output.success
    expected = {declared: input.params[declared]}
    client.call_tool.assert_awaited_once_with("inspect_job", expected)
    assert json.loads(request.action.args) == expected


async def test_mcp_argument_cleanup_does_not_silently_drop_unknown_business_arguments():
    tool = _tool(readOnlyHint=True)
    output = await tool.execute(ToolInput(params={"misspelled_query": "bug", "task_id": "t1", "workspace": "/tmp"}))
    assert output.success
    tool.client.call_tool.assert_awaited_once_with("search_issues", {"misspelled_query": "bug"})


async def test_mcp_mutations_still_use_the_central_gate_with_internal_context_present():
    store = rejecting_store()
    tool = bind_approvals(_tool(), store=store)
    output = await tool.execute(ToolInput(params={"title": "Update", "workspace": "/tmp", "task_id": "t1"}))
    assert not output.success and output.failure_kind == "refused"
    tool.client.call_tool.assert_not_awaited()
    store.wait_for_decision.assert_awaited_once()
