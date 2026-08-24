"""Async stdio client for Model Context Protocol (MCP) JSON-RPC 2.0 servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp.models import McpCallResult, McpServerConfig, McpToolDefinition

logger = logging.getLogger(__name__)


class McpError(Exception):
    """Raised when an MCP operation fails."""


class McpClient:
    """Manages an async stdio connection to a single MCP server process."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self.name = name
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._tools: list[McpToolDefinition] = []
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """Start the MCP server subprocess and perform the initialize handshake."""
        async with self._lock:
            if self._proc is not None:
                return

            env = {**os.environ, **self.config.env}
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self.config.command,
                    *self.config.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except Exception as exc:
                raise McpError(f"Failed to start MCP server {self.name!r}: {exc}") from exc

            self._reader_task = asyncio.create_task(self._read_loop())
            asyncio.create_task(self._log_stderr())

            # 1. Initialize handshake
            init_response = await self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "north", "version": "1.3.6"},
                },
            )
            logger.info("MCP server %r initialized: %s", self.name, init_response.get("serverInfo", {}))

            # 2. Initialized notification
            await self._send_notification("notifications/initialized", {})

            # 3. Discover available tools
            await self.refresh_tools()

    async def refresh_tools(self) -> list[McpToolDefinition]:
        """Query tools/list from the server and cache them."""
        result = await self._send_request("tools/list", {})
        raw_tools = result.get("tools", [])
        tools: list[McpToolDefinition] = []
        for t in raw_tools:
            try:
                tools.append(McpToolDefinition.model_validate(t))
            except Exception:
                logger.warning("MCP server %r returned invalid tool: %s", self.name, t)
        self._tools = tools
        return tools

    @property
    def tools(self) -> list[McpToolDefinition]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpCallResult:
        """Invoke tools/call on the MCP server."""
        if self._proc is None or self._closed:
            raise McpError(f"MCP server {self.name!r} is not running")

        result = await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=self.config.timeout,
        )
        return McpCallResult.model_validate(result)

    async def _send_request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise McpError(f"MCP server {self.name!r} is not running")

        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = fut

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        payload = json.dumps(msg).encode("utf-8") + b"\n"

        try:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
            to = timeout if timeout is not None else self.config.timeout
            response = await asyncio.wait_for(fut, timeout=to)
            if "error" in response:
                err = response["error"]
                raise McpError(f"MCP error ({err.get('code', -1)}): {err.get('message', 'Unknown error')}")
            return response.get("result", {})
        except TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise McpError(f"Timeout waiting for MCP method {method!r} on {self.name!r}") from None
        except Exception:
            self._pending_requests.pop(req_id, None)
            raise

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        payload = json.dumps(msg).encode("utf-8") + b"\n"
        self._proc.stdin.write(payload)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while not self._closed:
            line = await self._proc.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            req_id = data.get("id")
            if req_id is not None and req_id in self._pending_requests:
                fut = self._pending_requests.pop(req_id)
                if not fut.done():
                    fut.set_result(data)

    async def _log_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while not self._closed:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.debug("MCP [%s stderr]: %s", self.name, line.decode("utf-8", errors="replace").strip())

    async def close(self) -> None:
        """Gracefully close connection and terminate the subprocess."""
        self._closed = True
        for fut in self._pending_requests.values():
            if not fut.done():
                fut.cancel()
        self._pending_requests.clear()

        if self._reader_task:
            self._reader_task.cancel()

        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                self._proc.kill()
            self._proc = None
