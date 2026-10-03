"""A JSON-RPC client for `codex app-server` over stdio: one JSON message per line.

Requests are answered by id; everything else the server sends (notifications, and requests it makes of
us such as approvals) is handed to the caller in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from coding_agents.exceptions import CodingAgentError

logger = logging.getLogger(__name__)


class AppServerError(CodingAgentError):
    """The server answered a request with an error, or went away before it could."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class AppServer:
    """Talks to a started `codex app-server` process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None and process.stdout is not None
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        """Send a request and wait for its result."""
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        await self._send({"id": self._next_id, "method": method, "params": dict(params)})
        return await future

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._send({"method": method, **({"params": dict(params)} if params is not None else {})})

    async def respond(self, request_id: Any, result: Mapping[str, Any] | None = None, *, error: str = "") -> None:
        """Answer a request the server made of us: a result, or an error that the server reads as a refusal."""
        if error:
            await self._send({"id": request_id, "error": {"code": -32601, "message": error}})
        else:
            await self._send({"id": request_id, "result": dict(result or {})})

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Notifications and the server's own requests, until the server goes away."""
        while (message := await self._incoming.get()) is not None:
            yield message

    async def close(self) -> None:
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        self._fail_pending("the app-server was closed")
        with contextlib.suppress(Exception):
            self._stdin.close()

    async def _send(self, message: Mapping[str, Any]) -> None:
        try:
            self._stdin.write((json.dumps(message) + "\n").encode())
            await self._stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AppServerError("the app-server is gone") from exc

    async def _read(self) -> None:
        try:
            async for raw in self._stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    logger.debug("app-server printed a line that is not JSON: %r", raw[:120])
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" not in message and "id" in message:
                    self._answer(message)
                else:
                    await self._incoming.put(message)
        except ValueError as exc:  # a line past the stream limit
            logger.warning("app-server line exceeded the limit: %s", exc)
        finally:
            await self._incoming.put(None)
            self._fail_pending("the app-server exited")

    def _answer(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            future.set_exception(AppServerError(str(error.get("message") or message["error"]), error.get("code")))
        else:
            future.set_result(message.get("result"))

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AppServerError(reason))
        self._pending.clear()
