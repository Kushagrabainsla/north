"""Pydantic models for the Model Context Protocol (MCP) subsystem."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class McpServerConfig(BaseModel):
    """One MCP server: a local process north starts (`command`), or a hosted server north reaches (`url`)."""

    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # A hosted server, over Streamable HTTP. `headers` go with every request: a hosted server that
    # takes a token reads it from here ("Authorization": "Bearer ..."), as `env` holds a local one's.
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    timeout: float = 30.0

    @model_validator(mode="after")
    def _command_or_url(self) -> McpServerConfig:
        if bool(self.command) == bool(self.url):
            raise ValueError("an MCP server needs either a command (a local server) or a url (a hosted one)")
        if self.url and not self.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("a hosted MCP server's url must be https (or http on this machine)")
        return self


class McpConfigFile(BaseModel):
    """Schema for ~/.north/mcp.json and .north/mcp.json."""

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


class McpToolAnnotations(BaseModel):
    """What the server says a tool does (MCP tool annotations). Defaults are the spec's: assume the worst."""

    model_config = {"populate_by_name": True}

    title: str | None = None
    read_only_hint: bool = Field(default=False, alias="readOnlyHint")
    destructive_hint: bool = Field(default=True, alias="destructiveHint")
    idempotent_hint: bool = Field(default=False, alias="idempotentHint")
    open_world_hint: bool = Field(default=True, alias="openWorldHint")


class McpToolDefinition(BaseModel):
    """Metadata describing a single tool exposed by an MCP server."""

    model_config = {"populate_by_name": True}

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    annotations: McpToolAnnotations = Field(default_factory=McpToolAnnotations)


class McpCallResult(BaseModel):
    """Result of an MCP tools/call invocation."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = Field(default=False, alias="isError")
