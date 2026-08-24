"""Dynamic Tool adapter for Model Context Protocol (MCP) servers."""

from __future__ import annotations

from typing import Any

from mcp.client import McpClient, McpError
from mcp.models import McpToolDefinition
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


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
        self.is_mutating = True

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
