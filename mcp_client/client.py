"""One MCP server connection, over the official MCP SDK.

The SDK does the protocol: both eras (the 2024-2025 `initialize` handshake and the 2026-07-28 per-request
form, chosen per server by asking it first), stdio for a local server and Streamable HTTP for a hosted one.
north's own client spoke only the 2024-11-05 handshake over stdio, so a hosted server, or one that serves
only 2026-07-28, could not be reached (experiments/mcp/README.md).

This class keeps the interface north's tools and manager use: start, tools, call_tool, close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
from mcp_types.jsonrpc import CONNECTION_CLOSED

from coding_agents.environment import agent_environment
from mcp_client.models import McpCallResult, McpServerConfig, McpToolDefinition
from utils.version import NORTH_VERSION

logger = logging.getLogger(__name__)


class McpError(Exception):
    """Raised when an MCP operation fails."""


class McpClient:
    """A connection to one MCP server: a local process north starts, or a hosted server it reaches."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self.name = name
        self.config = config
        self._client: Client | None = None
        self._tools: list[McpToolDefinition] = []
        # The SDK's connection lives inside an async context. Its cancel scopes must be left by the task that
        # entered them, so one task holds the connection open from start() until close().
        self._holder: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._closed = False
        # Why the connection ended on its own (the server exited, or broke the protocol), or None while it is up.
        self._lost: str | None = None

    async def start(self) -> None:
        """Connect: start the server (or reach it), agree on a protocol version, and list its tools."""
        if self._holder is not None:
            return
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._holder = asyncio.create_task(self._hold(ready), name=f"mcp:{self.name}")
        try:
            await ready
        except BaseException:
            await self.close()
            raise
        await self.refresh_tools()

    async def _hold(self, ready: asyncio.Future[None]) -> None:
        stderr: _StderrLog | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                if self.config.url:
                    import httpx2

                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=self.config.headers, timeout=httpx2.Timeout(30.0, read=self.config.timeout)
                        )
                    )
                    transport: Any = streamable_http_client(self.config.url, http_client=http)
                else:
                    stderr = _StderrLog(self.name)
                    # A third-party tool server gets what a process needs plus its own `env` block, never
                    # north's: it inherited everything, NORTH_ANTHROPIC_API_KEY included.
                    env = {**agent_environment(network=True), **self.config.env}
                    params = StdioServerParameters(command=self.config.command, args=self.config.args, env=env)
                    transport = stdio_client(params, errlog=stderr.writer)
                client = Client(
                    transport,
                    read_timeout_seconds=self.config.timeout,
                    client_info=Implementation(name="north", version=NORTH_VERSION),
                )
                await asyncio.wait_for(stack.enter_async_context(client), self.config.timeout)
                if stderr is not None:
                    stderr.started()
                self._client = client
                logger.info(
                    "MCP server %r connected (protocol %s): %s",
                    self.name,
                    client.protocol_version,
                    client.server_info.name if client.server_info else "",
                )
                ready.set_result(None)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:  # an ExceptionGroup from the SDK's task group is a reason too
            reason = _reason(exc)
            if not ready.done():
                failed = McpError(f"Failed to start MCP server {self.name!r}: {reason}")
                failed.__cause__ = _root(exc)  # the manager words a missing command or a timeout from it
                ready.set_exception(failed)
            elif not self._closed:
                self._lost = reason
                logger.warning("MCP server %r connection lost: %s", self.name, reason)
        finally:
            self._client = None
            if stderr is not None:
                await stderr.close()
            if not self._closed and self._lost is None and ready.done() and not ready.cancelled():
                self._lost = "the server closed the connection"

    async def refresh_tools(self) -> list[McpToolDefinition]:
        """Ask the server for its tools and keep them."""
        client = self._live()
        tools: list[McpToolDefinition] = []
        cursor: str | None = None
        while True:
            try:
                page = await client.list_tools(cursor=cursor)
            except (MCPError, Exception) as exc:
                raise self._error(exc) from exc
            for tool in page.tools:
                try:
                    tools.append(McpToolDefinition.model_validate(tool.model_dump(by_alias=True, exclude_none=True)))
                except Exception:
                    logger.warning("MCP server %r returned an invalid tool: %s", self.name, tool.name)
            cursor = page.next_cursor
            if not cursor:
                break
        self._tools = tools
        return tools

    @property
    def tools(self) -> list[McpToolDefinition]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpCallResult:
        """Call one of the server's tools."""
        client = self._live()
        call = asyncio.ensure_future(client.call_tool(tool_name, arguments, read_timeout_seconds=self.config.timeout))
        lost = asyncio.ensure_future(self._lost_signal())
        try:
            # A server that dies mid-call fails the call now, not after the timeout.
            done, _ = await asyncio.wait({call, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            lost.cancel()
        if call not in done:
            call.cancel()
            raise McpError(f"MCP server {self.name!r} is not running: {self._lost}")
        try:
            result = call.result()
        except TimeoutError:
            raise McpError(f"Timeout waiting for MCP tool {tool_name!r} on {self.name!r}") from None
        except Exception as exc:
            raise self._error(exc) from exc
        content = [block.model_dump(by_alias=True, exclude_none=True) for block in result.content]
        return McpCallResult.model_validate({"content": content, "isError": result.is_error})

    async def _lost_signal(self) -> None:
        if self._holder is not None:
            await asyncio.shield(self._holder)

    def _live(self) -> Client:
        if self._lost is not None:
            raise McpError(f"MCP server {self.name!r} is not running: {self._lost}")
        if self._client is None or self._closed:
            raise McpError(f"MCP server {self.name!r} is not running")
        return self._client

    def _error(self, exc: BaseException) -> McpError:
        closed = isinstance(exc, MCPError) and exc.error.code == CONNECTION_CLOSED
        if closed or self._lost is not None or (self._holder is not None and self._holder.done()):
            why = self._lost or ("the server closed the connection" if closed else _reason(exc))
            return McpError(f"MCP server {self.name!r} is not running: {why}")
        if isinstance(exc, MCPError):
            return McpError(f"MCP error ({exc.error.code}): {exc.error.message}")
        return McpError(f"MCP server {self.name!r} failed: {_reason(exc)}")

    async def close(self) -> None:
        """Close the connection and stop the server process (a hosted server is only disconnected)."""
        self._closed = True
        self._stop.set()
        holder, self._holder = self._holder, None
        if holder is None:
            return
        try:
            # The SDK closes stdin, then terminates the process, then kills it.
            await asyncio.wait_for(asyncio.shield(holder), 10.0)
        except (TimeoutError, asyncio.CancelledError):
            holder.cancel()
            with contextlib.suppress(BaseException):
                await holder
        except BaseException:  # noqa: BLE001 - the server's own failure is not the closer's
            pass


def _root(exc: BaseException) -> BaseException:
    """The first real cause inside an exception group (the SDK's task groups wrap what went wrong)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _reason(exc: BaseException) -> str:
    """The first real cause inside an exception (group), in a few words."""
    exc = _root(exc)
    if isinstance(exc, FileNotFoundError):
        return f"the command was not found ({exc.filename or exc})"
    if isinstance(exc, TimeoutError):
        return "it did not answer in time"
    if isinstance(exc, MCPError):
        return f"MCP error ({exc.error.code}): {exc.error.message}"
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0]}" if text else type(exc).__name__


class _StderrLog:
    """A local server's stderr, line by line into north's debug log, as before the SDK.

    The SDK hands stderr to the child process as a file, so north gives it one end of a pipe and reads the other.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w")
        self._reader = os.fdopen(read_fd, "rb")
        self._task: asyncio.Task[None] | None = None

    def started(self) -> None:
        """The child holds its own copy of the write end now: drop north's, so the reader sees the end."""
        self.writer.close()
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=1024 * 1024)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), self._reader)
        try:
            while line := await reader.readline():
                logger.debug("MCP [%s stderr]: %s", self._name, line.decode("utf-8", errors="replace").rstrip())
        except (ValueError, OSError):
            pass  # a line longer than the limit, or the pipe closed under us: the log is best effort
        finally:
            transport.close()

    async def close(self) -> None:
        if not self.writer.closed:
            self.writer.close()
        if self._task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 2.0)
        if not self._reader.closed:
            self._reader.close()
