"""A tool north wrote, run in its own process, with its own packages, in north's sandbox.

North never imports the tool's code. `tools.learned_runner` loads it in the tool's own
environment (`tools.learned_env`) and serves it over MCP on stdio; this class is the tool
as north's agents, approvals and views see it, and talks to that process through north's
MCP client. The process starts on the first call and stops when the tool has been idle a
while (each one holds about 80 MB), unless the tool keeps a job running.

Under the OS sandbox the process reaches the network, but not the credential folders,
the keychain or north's own data; it writes only its own folders and temp; and it gets
only the secrets it declared. That is why its own word that a call changes nothing
(`read_only_when`) is honoured here, so a view can read from it: a tool that called
itself read-only once printed north's secret key from inside north's own process (issue
#76, M5). Without an OS sandbox, every call is a change, as for any tool north wrote.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from approval.approvals import Request
from config import secret_store
from mcp_client.client import McpClient, McpError
from mcp_client.models import McpServerConfig
from tools.base import Tool
from tools.learned_env import ToolEnvironmentError, ToolEnvironments
from tools.models import ToolInput, ToolOutput
from tools.specialized import _os_sandbox

logger = logging.getLogger(__name__)

RUNNER = Path(__file__).with_name("learned_runner.py")
# A tool's process stops after this long with no call, unless it keeps a job running.
IDLE_SECONDS = 600
CALL_TIMEOUT_SECONDS = 120.0
START_TIMEOUT_SECONDS = 60.0


def metadata_path(tool_file: Path) -> Path:
    """Where what the runner said about *tool_file* is kept, beside it."""
    return tool_file.with_suffix(".json")


class LearnedRuntime:
    """Where the tools north wrote keep their environments and their own files, and the sandbox they run in."""

    def __init__(self, learned_root: Path, sandbox: Any = "auto", environments: ToolEnvironments | None = None) -> None:
        self.environments = environments or ToolEnvironments(learned_root / "envs")
        self.data_root = learned_root / "data"
        self.sandbox = _os_sandbox.current() if sandbox == "auto" else sandbox

    def data_dir(self, tool: str) -> Path:
        return self.data_root / tool

    def remove(self, tool: str) -> None:
        """Delete *tool*'s environment and its own files: nothing of it is left."""
        self.environments.remove(tool)
        shutil.rmtree(self.data_dir(tool), ignore_errors=True)


class RunnerTool(Tool):
    """A tool north wrote, as everything in north calls it; its code runs in its own process."""

    is_mutating = True

    def __init__(self, tool_file: Path, meta: dict[str, Any], packages: list[str], runtime: LearnedRuntime) -> None:
        self.tool_file = tool_file
        self.meta = meta
        self.packages = list(packages)
        self.runtime = runtime
        self.name = str(meta["name"])
        self.description = str(meta.get("description") or "")
        self.parameters_schema = meta.get("parameters_schema") or {"type": "object", "properties": {}}
        self.uses_secrets = tuple(str(name) for name in meta.get("uses_secrets") or ())
        self._read_only_when: dict[str, list[Any]] = dict(meta.get("read_only_when") or {})
        self._keeps_running = bool(meta.get("keeps_running"))
        self._client: McpClient | None = None
        self._secrets_seen: tuple[tuple[str, str], ...] = ()
        self._lock = asyncio.Lock()
        self._idle: asyncio.Task[None] | None = None

    @property
    def sandboxed(self) -> bool:
        return self.runtime.sandbox is not None

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        if not self.sandboxed:
            return True  # its own word is trusted only inside the sandbox
        params = params or {}
        if any(params.get(key) in values for key, values in self._read_only_when.items()):
            return False
        return bool(self.meta.get("is_mutating", True))

    async def describe(self, input: ToolInput) -> Request | None:
        request = await super().describe(input)
        if request is None:
            return None
        uses = f"\nIt uses your secrets: {', '.join(self.uses_secrets)}." if self.uses_secrets else ""
        return replace(
            request,
            title=f"Run {self.name} - Approval Required",
            message=f"Run `{self.name}`, a tool north wrote for itself, with:\n{request.message}{uses}",
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        params = json.loads(json.dumps(input.params, default=str))
        try:
            async with self._lock:
                client = await self._started()
            result = await client.call_tool("run", {"params": params})
        except (McpError, ToolEnvironmentError) as exc:
            await self.aclose()
            return ToolOutput(success=False, error=f"{self.name} could not run: {exc}")
        finally:
            self._rest()
        text = "".join(str(block.get("text") or "") for block in result.content if block.get("type") == "text")
        try:
            out = json.loads(text)
        except ValueError:
            return ToolOutput(success=False, error=f"{self.name} answered with something north cannot read.")
        if result.is_error or not isinstance(out, dict):
            return ToolOutput(success=False, error=text[:500] or f"{self.name} failed.")
        data = out.get("data")
        return ToolOutput(
            success=bool(out.get("success")),
            data=data if isinstance(data, dict) else {"result": data},
            error=out.get("error"),
            failure_kind=out.get("failure_kind"),
        )

    async def _started(self) -> McpClient:
        """The running process, started first; started again when a secret it declared was saved since."""
        seen = self._secret_stamp()
        if self._client is not None and seen != self._secrets_seen:
            await self._stop()
        if self._client is not None:
            return self._client
        argv, env = await self.command()
        config = McpServerConfig(command=argv[0], args=argv[1:], env=env, timeout=CALL_TIMEOUT_SECONDS)
        client = McpClient(f"tool:{self.name}", config)
        await asyncio.wait_for(client.start(), START_TIMEOUT_SECONDS)
        self._client, self._secrets_seen = client, seen
        logger.info("Tool %s: started in its own process%s", self.name, "" if self.sandboxed else " (no OS sandbox)")
        return client

    async def command(self, *runner_args: str) -> tuple[list[str], dict[str, str]]:
        """The argv that runs the runner for this tool (in the sandbox when there is one), and its environment.

        The environment holds only the secrets the tool declared; `McpClient` adds no more than a worker needs.
        """
        python = await self.runtime.environments.python_for(self.name, self.packages)
        data = self.runtime.data_dir(self.name)
        data.mkdir(parents=True, exist_ok=True)
        # Its own copy, inside its own folder: the sandbox keeps north's home closed.
        code = data / "tool.py"
        code.write_text(self.tool_file.read_text(encoding="utf-8"), encoding="utf-8")
        argv = [str(python), str(RUNNER), *runner_args, str(code)]
        if self.runtime.sandbox is not None:
            # Writes: its own folder and environment, temp and the shared caches. Reads: all but the credential
            # folders, the keychain and the rest of north's home. The network is open: a tool calls web APIs.
            own = (str(data), str(python.parent.parent))
            argv = self.runtime.sandbox.wrap(shlex.join(argv), str(data), writable=True, north_folders=own)
        store = secret_store.store()
        secrets = {name: value for name in self.uses_secrets if (value := store.value(name)) is not None}
        return argv, {"NORTH_TOOL_SECRETS": json.dumps(secrets), "HOME": str(Path.home())}

    def _secret_stamp(self) -> tuple[tuple[str, str], ...]:
        saved = {item.name: item.updated_at for item in secret_store.store().saved()}
        return tuple((name, saved.get(name, "")) for name in self.uses_secrets)

    def _rest(self) -> None:
        """Stop the process after IDLE_SECONDS with no call, unless the tool keeps a job running."""
        if self._idle is not None:
            self._idle.cancel()
        if self._keeps_running:
            return
        with contextlib.suppress(RuntimeError):
            self._idle = asyncio.get_running_loop().create_task(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        await asyncio.sleep(IDLE_SECONDS)
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
            logger.info("Tool %s: process stopped", self.name)

    async def aclose(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        await self._stop()


async def describe_file(tool_file: Path, name: str, packages: list[str], runtime: LearnedRuntime) -> dict[str, Any]:
    """What the tool in *tool_file* says it is, read by the runner in its own environment and sandbox.

    Raises ValueError with the runner's own words when it cannot load: an import its environment lacks
    names the module, so the fix (add its package) is clear.
    """
    probe = RunnerTool(tool_file, {"name": name}, packages, runtime)
    try:
        argv, env = await probe.command("--describe")
    except ToolEnvironmentError as exc:
        raise ValueError(str(exc)) from None
    from coding_agents.environment import agent_environment

    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**agent_environment(network=True), **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), START_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        raise ValueError(f"the tool did not load within {START_TIMEOUT_SECONDS:.0f} s") from None
    if process.returncode != 0:
        said = stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(said[-1] if said else "the tool could not be loaded")
    return json.loads(stdout.decode())
