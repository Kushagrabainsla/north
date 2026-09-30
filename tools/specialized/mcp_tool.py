"""Dynamic Tool adapter for Model Context Protocol (MCP) servers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.client import McpClient, McpError
from mcp.models import McpToolDefinition
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from approval.approvals import Request


class McpTool(Tool):
    """Wraps an external tool exposed by an MCP server."""

    def __init__(self, server_name: str, client: McpClient, tool_def: McpToolDefinition) -> None:
        self.server_name = server_name
        self.client = client
        self.tool_def = tool_def
        self._original_name = tool_def.name

        self.name = f"mcp__{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"MCP tool '{tool_def.name}' from '{server_name}' server"
        self.parameters_schema = tool_def.input_schema or {"type": "object", "properties": {}}
        # The server's own word on whether the tool only reads. A server is one you
        # configured, so its read-only hint is trusted; without it, every call is
        # treated as a change and goes to the approval layer.
        self.is_mutating = not tool_def.annotations.read_only_hint

    async def describe(self, input: ToolInput) -> Request:
        """The call as facts: which server and tool, its arguments, and what the server says it does."""
        from approval.approvals import Request
        from approval.policy import Action, ActionKind

        generic = await super().describe(input)
        args = generic.action.args
        hints = self.tool_def.annotations
        said = [
            line
            for line, present in (
                ("The server says it may delete or overwrite things.", hints.destructive_hint),
                ("The server says it reaches outside your machine.", hints.open_world_hint),
            )
            if present
        ]
        name = hints.title or self._original_name
        message = "\n".join([f"**{name}** on MCP server `{self.server_name}`", f"```json\n{args}\n```", *said])
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.MCP,
                summary=f"{self.server_name}: {self._original_name} {args}",
                operation=self._original_name,
                command=self.server_name,
                args=args,
                details="\n".join(said),
            ),
            title=f"{name} ({self.server_name}) - Approval Required",
            message=message,
        )

    def format_output(self, data: dict[str, Any]) -> str:
        text = data.get("text")
        if text:
            return str(text)
        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        try:
            result = await self.client.call_tool(self._original_name, input.params)
        except McpError as exc:
            return ToolOutput(success=False, error=f"MCP tool {self.name!r} failed: {exc}")
        except Exception as exc:
            return ToolOutput(success=False, error=f"Unexpected error in MCP tool {self.name!r}: {exc}")

        if result.is_error:
            error_msgs = []
            for item in result.content:
                if item.get("type") == "text":
                    error_msgs.append(item.get("text", ""))
            return ToolOutput(success=False, error="\n".join(error_msgs) or "MCP tool returned an error")

        texts = []
        for item in result.content:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))

        full_text = "\n".join(texts)
        return ToolOutput(
            success=True,
            data={"text": full_text, "raw_content": result.content},
        )
