"""Throwaway stand-ins for the pieces the design adds. Nothing in product code imports this.

- `gate_shim`    : the fail-closed Claude PreToolUse hook (becomes `coding_agents/hook.py`)
- `GateServer`   : the loopback route the shim posts to (becomes a `coding_agents` router)
- `CodingTool`   : a `Tool` with the shape `coding_agent` will have
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import textwrap
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi import Request as HttpRequest

from approval.approvals import Approvals, Request
from approval.policy import Action, ActionKind
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

GATE_SHIM_SOURCE = textwrap.dedent(
    """
    import json, os, sys, urllib.request

    def fail(reason):
        sys.stderr.write("north gate: " + reason + "\\n")
        sys.exit(2)  # exit 2 is the only failure Claude Code treats as a block

    try:
        payload = sys.stdin.read()
        request = urllib.request.Request(
            os.environ["NORTH_GATE_URL"],
            data=payload.encode(),
            headers={"X-Gate-Token": os.environ["NORTH_GATE_TOKEN"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:  # no client timeout: cards wait
            body = response.read().decode()
        decision = json.loads(body)["hookSpecificOutput"]["permissionDecision"]
        if decision not in ("allow", "deny"):
            fail("unknown decision " + repr(decision))
        sys.stdout.write(body)
    except SystemExit:
        raise
    except Exception as exc:  # any error at all means deny
        fail(type(exc).__name__ + ": " + str(exc))
    """
)


def write_shim(directory: Path) -> Path:
    path = directory / "gate_shim.py"
    path.write_text(GATE_SHIM_SOURCE)
    return path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hook_payload(tool_name: str, tool_input: dict[str, Any], cwd: str) -> dict[str, Any]:
    """The JSON Claude Code sends a PreToolUse hook (field names from the lab)."""
    return {
        "session_id": "s1",
        "cwd": cwd,
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }


def action_for(tool_name: str, tool_input: dict[str, Any], workspace: str) -> Action:
    """Hook payload -> approval Action, the way `coding_agents/gate.py` will."""
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        read_only = command.startswith(("git status", "ls", "cat "))
        return Action(
            agent="coding:claude",
            kind=ActionKind.SHELL_COMMAND,
            summary=command,
            command=command,
            read_only=read_only,
            mutating=not read_only,
        )
    if tool_name in ("Write", "Edit", "MultiEdit"):
        path = Path(str(tool_input.get("file_path", "")))
        return Action(
            agent="coding:claude",
            kind=ActionKind.FILE_EDIT,
            summary=f"{tool_name} {path}",
            path=path,
            workspace=workspace,
        )
    return Action(agent="coding:claude", kind=ActionKind.OTHER, summary=f"{tool_name}", mutating=True)


class GateServer:
    """A loopback FastAPI app with the one route the shim talks to, run on the caller's event loop."""

    def __init__(self, approvals: Approvals, tokens: dict[str, str], *, garbage: bool = False) -> None:
        self.port = free_port()
        self._tokens = tokens  # token -> task_id of the run that owns it
        self._garbage = garbage
        self._approvals = approvals
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/gate"

    def _app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/gate")
        async def gate(request: HttpRequest, x_gate_token: str | None = Header(default=None)) -> Any:
            task_id = self._tokens.get(x_gate_token or "")
            if task_id is None:
                raise HTTPException(status_code=403, detail="unknown run token")
            if self._garbage:
                return {"nonsense": True}
            body = await request.json()
            action = action_for(body["tool_name"], body["tool_input"], body["cwd"])
            decision = await self._approvals.decide(
                Request(action, f"{action.agent}: {action.kind.value}", action.summary), task_id=task_id
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if decision.allowed else "deny",
                    "permissionDecisionReason": decision.reason,
                }
            }

        return app

    async def start(self) -> None:
        config = uvicorn.Config(self._app(), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task


async def run_shim(shim: Path, url: str, token: str, payload: dict[str, Any]) -> tuple[int, str, str]:
    """Run the real shim as Claude Code would: payload on stdin, answer on stdout."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(shim),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"NORTH_GATE_URL": url, "NORTH_GATE_TOKEN": token, "PATH": "/usr/bin:/bin"},
    )
    out, err = await proc.communicate(json.dumps(payload).encode())
    return proc.returncode or 0, out.decode(), err.decode()


class CodingTool(Tool):
    """The shape of `coding_agent`: mutating, described to the approval layer before it runs."""

    name = "coding_agent"
    description = "Run the installed Claude Code or Codex on a task in an isolated worktree."
    is_mutating = True

    def __init__(self) -> None:
        self.ran_with: dict[str, Any] | None = None

    async def describe(self, input: ToolInput) -> Request | None:
        p = input.params
        summary = f"Run {p.get('backend', 'claude')} ({p.get('mode', 'plan')}) in {p.get('workspace')}: {p.get('task')}"
        return Request(
            Action(agent=self.name, kind=ActionKind.OTHER, summary=summary, workspace=str(p.get("workspace", ""))),
            "Coding Agent - Approval Required",
            summary,
            prepared={"backend": p.get("backend", "claude")},
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        self.ran_with = dict(input.params)
        return ToolOutput(success=True, data={"granted": input.granted_workspace})


async def until(predicate, *, timeout: float = 5.0):
    """Poll until *predicate* returns something truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not met")


_RESULTS = Path(__file__).parent / "results.json"


def record(key: str, data: Any) -> None:
    """Merge one experiment's observations into results.json (recorded evidence)."""
    current = json.loads(_RESULTS.read_text()) if _RESULTS.exists() else {}
    current[key] = data
    _RESULTS.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
