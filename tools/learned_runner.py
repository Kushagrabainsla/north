"""Serve one tool north wrote, over MCP on stdio, inside the tool's own environment.

North starts this file with the tool's own Python (`tools.learned_env`), under its OS
sandbox, and talks to it through its MCP client (`tools.learned_remote`). It imports
nothing of north's: the tool's environment has only its own packages and the MCP SDK.
So the names a tool imports from north (`tools.base.Tool`, `tools.models.ToolInput`
and `ToolOutput`, `config.secret_store.secret`) are small stand-ins defined here, with
the same shape. A tool gets the secrets it declared, and no others, from north at start.

    python learned_runner.py <tool file>               serve it
    python learned_runner.py --describe <tool file>    print what it is, or why it cannot load

MCP tools served: `describe` (what the tool is) and `run` (one call). Which calls change
nothing is read from what `describe` declares (`read_only_when`), never from the tool's own
`mutates()`: north decides that, and honours it only while the tool runs in the sandbox.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import types
from typing import Any


def _take_secrets() -> dict[str, str]:
    """The secrets this tool declared, {name: value}, as north passed them; taken out of the environment, so a
    program the tool starts does not inherit them."""
    return json.loads(os.environ.pop("NORTH_TOOL_SECRETS", "{}") or "{}")


_SECRETS: dict[str, str] = _take_secrets()


class SecretUnavailable(LookupError):
    """A secret this tool did not declare, or one the user has not saved."""


def secret(name: str) -> str:
    """The value of the user's secret *name*, which the tool must declare in `uses_secrets`."""
    if name not in _SECRETS:
        raise SecretUnavailable(
            f"North has no secret named {name!r} for this tool. Declare it in `uses_secrets`, and call "
            f"request_secret with name {name!r} if the user has not saved it."
        )
    return _SECRETS[name]


class ToolInput:
    """One call's arguments, as `input.params`."""

    def __init__(self, params: dict[str, Any] | None = None, **_: Any) -> None:
        self.params: dict[str, Any] = dict(params or {})


class ToolOutput:
    """What a call returns: success, data, and an error to show when it failed."""

    def __init__(
        self, success: bool, data: Any = None, error: str | None = None, failure_kind: str | None = None, **_: Any
    ) -> None:
        self.success = bool(success)
        self.data = {} if data is None else data
        self.error = error
        self.failure_kind = failure_kind

    def as_dict(self) -> dict[str, Any]:
        return {"success": self.success, "data": self.data, "error": self.error, "failure_kind": self.failure_kind}


class Tool:
    """The base a tool north wrote subclasses: the shape of north's own, for the parts a tool uses."""

    name = ""
    description = ""
    parameters_schema: dict[str, Any] = {"type": "object", "properties": {}}
    is_mutating = False
    uses_secrets: tuple[str, ...] = ()
    # {parameter: [values]}: a call with one of these values changes nothing, such as {"action": ["list"]}.
    read_only_when: dict[str, list[Any]] = {}
    # True for a tool that runs a job between calls (music sync): north does not stop its process when idle.
    keeps_running = False

    def format_output(self, data: dict[str, Any]) -> str:
        return json.dumps(data, default=str)

    async def run(self, input: ToolInput) -> ToolOutput:
        raise NotImplementedError


def _install_stand_ins() -> None:
    """Make `tools.base`, `tools.models` and `config.secret_store` importable as the stand-ins above."""
    modules: dict[str, dict[str, Any]] = {
        "tools": {},
        "tools.base": {"Tool": Tool},
        "tools.models": {"ToolInput": ToolInput, "ToolOutput": ToolOutput},
        "config": {},
        "config.secret_store": {"secret": secret, "SecretUnavailable": SecretUnavailable},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(members)
        if "." not in name:
            module.__path__ = []  # a package, so its submodules import
        sys.modules[name] = module


def load(path: str) -> Tool:
    """The one tool class in the file at *path*, built, working in that file's folder: the tool's own,
    where the sandbox lets it keep files (a relative path such as "state.json" lands there)."""
    os.chdir(os.path.dirname(os.path.abspath(path)))
    _install_stand_ins()
    spec = importlib.util.spec_from_file_location("north_runner_tool", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Python cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classes = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Tool) and obj is not Tool and not inspect.isabstract(obj)
    ]
    if len(classes) != 1:
        raise ValueError(f"The file defines {len(classes)} tool classes; it must define exactly one.")
    return classes[0]()


def describe(tool: Tool) -> dict[str, Any]:
    declared = tool.uses_secrets
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters_schema": tool.parameters_schema,
        "is_mutating": bool(tool.is_mutating),
        "read_only_when": tool.read_only_when or {},
        "keeps_running": bool(getattr(tool, "keeps_running", False)),
        "uses_secrets": [declared] if isinstance(declared, str) else list(declared),
    }


async def call(tool: Tool, params: dict[str, Any]) -> dict[str, Any]:
    """One call, as a dict; an exception is a failed call, never a crash of the runner."""
    try:
        output = await tool.run(ToolInput(params=params))
    except SecretUnavailable as exc:
        return ToolOutput(success=False, error=str(exc)).as_dict()
    except Exception as exc:
        return ToolOutput(success=False, error=f"{type(exc).__name__}: {exc}").as_dict()
    if not isinstance(output, ToolOutput):
        return ToolOutput(success=False, error="The tool did not return a ToolOutput.").as_dict()
    return output.as_dict()


def serve(path: str) -> None:
    from mcp.server.mcpserver import MCPServer

    tool = load(path)
    server = MCPServer(f"north-tool-{tool.name}", version="1")

    @server.tool(name="describe")
    def _describe() -> str:
        """What the tool is: its name, description, parameters, secrets and read-only calls."""
        return json.dumps(describe(tool))

    @server.tool(name="run")
    async def _run(params: dict[str, Any]) -> str:
        """Make one call of the tool."""
        return json.dumps(await call(tool, params), default=str)

    server.run()


def describe_and_exit(path: str) -> None:
    """`--describe`: print what the tool is, or why it cannot load, and exit. North checks a draft this way."""
    try:
        print(json.dumps(describe(load(path))))
    except BaseException as exc:  # an import the environment lacks, a syntax error, two classes...
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    if sys.argv[1] == "--describe":
        describe_and_exit(sys.argv[2])
    else:
        serve(sys.argv[1])
