"""Private Puppeteer Core adapter using North's existing MCP transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from config.browser_connection import cdp_http_base
from mcp.client import McpClient, McpError
from mcp.models import McpServerConfig
from tools.models import ToolOutput
from utils.runtime_resources import resource_path
from utils.version import NORTH_VERSION

if TYPE_CHECKING:
    from config.browser_profiles import BrowserProfile

_INSTALL_TIMEOUT_SECONDS = 120
_CALL_TIMEOUT_SECONDS = 135


class ExistingChrome:
    """One private SDK bridge; profiles and task windows are not exposed as MCP tools."""

    def __init__(self, north_home: Path) -> None:
        self._north_home = north_home
        self._client: McpClient | None = None
        self._lock = asyncio.Lock()

    async def _runtime(self) -> Path:
        bundle = resource_path("browser")
        lock = await asyncio.to_thread((bundle / "package-lock.json").read_bytes)
        root = self._north_home / "runtime" / "browser" / hashlib.sha256(lock).hexdigest()[:16]
        if (root / "installed").is_file():
            return root
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("Existing Chrome needs Node >=22.12 and npm. No other browser will be launched.")
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            await asyncio.to_thread(shutil.copyfile, bundle / name, root / name)
        await self._install(root, npm)
        await asyncio.to_thread((root / "installed").write_text, "complete", encoding="utf-8")
        return root

    async def _install(self, root: Path, npm: str) -> None:
        process = await asyncio.create_subprocess_exec(
            npm,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(), _INSTALL_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("Could not install the pinned existing-Chrome SDK. Check npm/network and retry.")

    async def _start(self) -> McpClient:
        if self._client is not None:
            return self._client
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Existing Chrome needs Node >=22.12. North will not fall back to another browser.")
        root = await self._runtime()
        client = self._new_client(root, node)
        try:
            await client.start()
        except BaseException:
            await client.close()
            raise
        self._client = client
        return client

    def _new_client(self, root: Path, node: str) -> McpClient:
        return McpClient(
            "north-existing-chrome",
            McpServerConfig(
                command=node,
                args=[
                    str(resource_path("browser/server.mjs")),
                    "--sdk-root",
                    str(root),
                    "--state-dir",
                    str(self._north_home / "browser" / "task-windows"),
                    "--version",
                    NORTH_VERSION,
                ],
                timeout=_CALL_TIMEOUT_SECONDS,
            ),
        )

    async def _endpoint(self, connect: str) -> str:
        base = cdp_http_base(connect)
        if urlparse(connect).scheme in {"ws", "wss"}:
            return connect
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{base}/json/version")
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("CDP discovery response was too large.")
            endpoint = response.json()["webSocketDebuggerUrl"]
        cdp_http_base(endpoint)
        if urlparse(endpoint).scheme not in {"ws", "wss"}:
            raise ValueError("CDP discovery did not provide a WebSocket.")
        return endpoint

    async def call(self, profile: BrowserProfile, params: dict) -> ToolOutput:
        """Run only after BrowserTool's central approval and profile-config checks."""
        if not params.get("task_id"):
            return ToolOutput(success=False, error="An existing-browser call requires a task_id for window ownership.")
        return await self._invoke(self._arguments(profile, params))

    def _arguments(self, profile: BrowserProfile, params: dict) -> dict:
        return {
            "task_id": str(params.get("task_id") or "default"),
            "profile_id": profile.id,
            "profile_config": hashlib.sha256(profile.model_dump_json().encode()).hexdigest(),
            "expected_path": str(profile.expected_path),
            "endpoint": str(params["connect"]),
            "params": {key: value for key, value in params.items() if key not in {"workspace", "connect"}},
        }

    async def _invoke(self, arguments: dict) -> ToolOutput:
        async with self._lock:
            try:
                return await self._dispatch(arguments)
            except asyncio.CancelledError:
                await self._close_client()
                raise
            except (McpError, OSError, RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
                # A transport timeout is not evidence of failure before the action.
                # Terminate the worker, never retry: its durable in-flight marker remains.
                await self._close_client()
                return ToolOutput(
                    success=False,
                    error=f"Existing Chrome could not complete this call ({type(exc).__name__}). "
                    "Check the connection and inspect the task window before retrying any action.",
                )

    async def _dispatch(self, arguments: dict) -> ToolOutput:
        endpoint = await self._endpoint(arguments["endpoint"])
        arguments["endpoint"] = endpoint
        client = await self._start()
        timeout = min(max(float(arguments["params"].get("timeout_seconds", 30)), 1), 120) + 5
        result = await asyncio.wait_for(client.call_tool("browser_action", arguments), timeout=timeout)
        text = "\n".join(block.get("text", "") for block in result.content if block.get("type") == "text")
        if result.is_error:
            return ToolOutput(success=False, error=text)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Expected an object from the browser adapter.")
        if arguments["params"]["action"] != "close" and data.get("profile_verified") is not True:
            return ToolOutput(success=False, error="The browser adapter did not prove profile identity.")
        data.update(connection_id=hashlib.sha256(endpoint.encode()).hexdigest(), endpoint=cdp_http_base(endpoint))
        if data.get("held") is False:
            return ToolOutput(success=False, data=data, error="Browser assertion was not met.")
        return ToolOutput(success=True, data=data)

    def description_runner(self, profile: BrowserProfile, params: dict):
        """Adapt page reads to the existing shared browser-action description code."""

        async def read_page(command: list[str], timeout: int):
            action, *arguments = command
            values = {**params, "action": action, "timeout_seconds": timeout}
            if action == "eval":
                values["js"] = arguments[0]
            arguments = self._arguments(profile, values)
            arguments["purpose"] = "description"
            output = await self._invoke(arguments)
            if not output.success:
                raise RuntimeError("Could not read the verified task page.")
            return SimpleNamespace(stdout=json.dumps(output.data))

        return read_page

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def aclose(self) -> None:
        """Disconnect the SDK without closing Chrome or unreviewed task windows."""
        async with self._lock:
            await self._close_client()
