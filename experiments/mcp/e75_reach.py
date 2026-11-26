"""#75: which MCP servers can north's client reach, and which can the official SDK client reach?

Each probe starts one server, lists its tools, and (for the probe servers) calls `echo` and `big` (256 KB).
It records only counts, times and error classes, never tool output.

Two clients:
  north  north's client from a checkout: main's own `mcp/client.py`, or this branch's `mcp_client/` (over the SDK).
  sdk    the official Python SDK 2.3.0 `Client` alone (mode "auto"), from the probe venv.

    # before: main, in its own worktree and venv (main's `mcp/` folder and the SDK cannot share one venv)
    uv run --project <main worktree> python experiments/mcp/e75_reach.py north <main worktree> north_main
    # after: this branch
    .venv/bin/python experiments/mcp/e75_reach.py north <this checkout> north_branch

Setup (once): see README.md. Results go to experiments/mcp/results/e75_reach_<label>.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROBE_HOME = Path.home() / ".north" / "experiments" / "mcp"
SDK_PY = str(PROBE_HOME / "sdkvenv" / "bin" / "python")
NODE_V1 = str(PROBE_HOME / "node" / "node_modules")
NODE_V2 = str(PROBE_HOME / "node2" / "node_modules")
MCP_REMOTE = str(PROBE_HOME / "node" / "node_modules" / "mcp-remote" / "dist" / "proxy.js")
PY_SERVER = str(HERE / "servers" / "py_server.py")
TS_SERVER = str(HERE / "servers" / "ts_server.mjs")
LEGACY_SERVER = str(HERE / "servers" / "legacy_silent.py")
DEEPWIKI = "https://mcp.deepwiki.com/mcp"
# A public repository: nothing private goes to the hosted server.
DEEPWIKI_REPO = "modelcontextprotocol/python-sdk"
BIG_KB = 256
DEVNULL = open(os.devnull, "w")  # noqa: SIM115 - lives as long as the probe


def servers() -> dict[str, dict[str, Any]]:
    """mcp.json entries: local servers (command + args + env) and one hosted server (url)."""
    servers: dict[str, dict[str, Any]] = {
        "python-sdk-2.3 (default)": {"command": SDK_PY, "args": [PY_SERVER]},
        "python-sdk-2.3 (2026-07-28 only)": {"command": SDK_PY, "args": [PY_SERVER, "--modern"]},
        "typescript-sdk-1.32": {"command": "node", "args": [TS_SERVER, "v1"], "env": {"NODE_PATH": NODE_V1}},
        "typescript-server-2.4": {"command": "node", "args": [TS_SERVER, "v2"], "env": {"NODE_PATH": NODE_V2}},
        "hand-written 2024-11-05, answers unknown methods": {
            "command": sys.executable,
            "args": [LEGACY_SERVER, "--answers"],
        },
        "hand-written 2024-11-05, silent on unknown methods": {"command": sys.executable, "args": [LEGACY_SERVER]},
        "deepwiki via mcp-remote": {"command": "node", "args": [MCP_REMOTE, DEEPWIKI, "--transport", "http-only"]},
        "deepwiki by url": {"url": DEEPWIKI},
    }
    # The owner's configured servers, as north starts them (handshake and tools/list only; no calls).
    config = Path.home() / ".north" / "mcp.json"
    if config.is_file():
        for name, srv in json.loads(config.read_text()).get("mcpServers", {}).items():
            if not srv.get("disabled"):
                servers[f"configured: {name}"] = {k: srv[k] for k in ("command", "args", "env") if k in srv}
    return servers


def calls_for(name: str) -> list[tuple[str, dict[str, Any]]]:
    if name.startswith("configured:"):
        return []
    if name.startswith("deepwiki"):
        return [("read_wiki_structure", {"repoName": DEEPWIKI_REPO})]
    return [("echo", {"text": "hello"}), ("big", {"kilobytes": BIG_KB})]


def error_class(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = f"{type(exc).__name__}: {exc}"
    return text.splitlines()[0][:160]


# ---------------------------------------------------------------- north's client


async def probe_north(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    try:  # this branch: north's client is mcp_client/, over the SDK
        from mcp_client.client import McpClient
        from mcp_client.models import McpServerConfig
    except ImportError:  # main: north's own client in mcp/
        from mcp.client import McpClient  # type: ignore[no-redef]
        from mcp.models import McpServerConfig  # type: ignore[no-redef]

    out: dict[str, Any] = {"server": name}
    started = time.perf_counter()
    try:
        config = McpServerConfig(timeout=60, **spec)
    except Exception as exc:  # main has no field for a url: the entry cannot even be read
        return {**out, "ok": False, "start_s": 0.0, "error": error_class(exc)}
    client = McpClient(name, config)
    try:
        await asyncio.wait_for(client.start(), 90)
        out["start_s"] = round(time.perf_counter() - started, 2)
        out["tools"] = len(client.tools)
        out["calls"] = []
        for tool, args in calls_for(name):
            t = time.perf_counter()
            try:
                result = await client.call_tool(tool, args)
                size = sum(len(c.get("text", "")) for c in result.content)
                out["calls"].append({"tool": tool, "ok": not result.is_error, "chars": size, "s": _since(t)})
            except Exception as exc:
                out["calls"].append({"tool": tool, "ok": False, "error": error_class(exc), "s": _since(t)})
        out["ok"] = all(c["ok"] for c in out["calls"])
    except Exception as exc:
        out.update(ok=False, start_s=_since(started), error=error_class(exc))
    finally:
        await client.close()
    return out


# ---------------------------------------------------------------- the SDK's client


async def probe_sdk(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    out: dict[str, Any] = {"server": name}
    started = time.perf_counter()
    try:
        if "url" in spec:
            from mcp.client.streamable_http import streamable_http_client

            transport: Any = streamable_http_client(spec["url"])
        else:
            transport = stdio_client(
                StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env")),
                errlog=DEVNULL,  # servers log private details (an account name) to stderr
            )
        async with asyncio.timeout(90), Client(transport, mode="auto") as client:
            out["start_s"] = _since(started)
            out["protocol"] = client.protocol_version
            tools = await client.list_tools()
            out["tools"] = len(tools.tools)
            out["calls"] = []
            for tool, args in calls_for(name):
                t = time.perf_counter()
                try:
                    result = await client.call_tool(tool, args)
                    size = sum(len(getattr(c, "text", "")) for c in result.content)
                    out["calls"].append({"tool": tool, "ok": not result.is_error, "chars": size, "s": _since(t)})
                except Exception as exc:
                    out["calls"].append({"tool": tool, "ok": False, "error": error_class(exc), "s": _since(t)})
            out["ok"] = all(c["ok"] for c in out["calls"])
    except BaseException as exc:  # noqa: BLE001 - an ExceptionGroup from anyio is a result too
        out.update(ok=False, start_s=_since(started), error=error_class(exc))
    return out


def _since(t: float) -> float:
    return round(time.perf_counter() - t, 2)


async def main(which: str, label: str) -> None:
    results = []
    for name, spec in servers().items():
        results.append(await (probe_north(name, spec) if which == "north" else probe_sdk(name, spec)))
        print(json.dumps(results[-1]))
    out = HERE / "results" / f"e75_reach_{label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"{sum(r['ok'] for r in results)}/{len(results)} reached -> {out}")


if __name__ == "__main__":
    # north [REPO LABEL]: north's client from REPO (default: this checkout), results named e75_reach_LABEL.json.
    which = sys.argv[1]
    repo = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent.parent
    label = sys.argv[3] if len(sys.argv) > 3 else which
    if which == "north":
        sys.path.insert(0, str(repo))
    asyncio.run(main(which, label))
