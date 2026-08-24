"""Pydantic models for the Model Context Protocol (MCP) subsystem."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server process."""

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    timeout: float = 30.0


class McpConfigFile(BaseModel):
    """Schema for ~/.north/mcp.json and .north/mcp.json."""

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


class McpToolDefinition(BaseModel):
    """Metadata describing a single tool exposed by an MCP server."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")


class McpCallResult(BaseModel):
    """Result of an MCP tools/call invocation."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = Field(default=False, alias="isError")
