# mcp_client

This package is the client side of the Model Context Protocol (MCP) in north. It starts the local MCP servers and
connects to the hosted MCP servers that `mcp.json` names, and it gives their tools to the tool registry. The official
`mcp` SDK (pinned in `pyproject.toml`) does the protocol work. This package only adapts the SDK to north.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `models.py` | `McpServerConfig`, `McpConfigFile`, `McpToolDefinition`, `McpToolAnnotations`, `McpCallResult` | Pydantic models for `mcp.json`, a tool that a server offers, and the result of a tool call. |
| `client.py` | `McpClient`, `McpError` | One connection to one server. `start`, `refresh_tools`, `tools`, `call_tool`, `close`. |
| `manager.py` | `McpManager`, `UNAVAILABLE_PREFIX` | Finds and merges the config files, starts all servers at the same time, and registers their tools. |

`McpServerConfig` holds one server. A server has a `command` (a local process over stdio) or a `url` (a hosted
server over Streamable HTTP), but not both. A `url` must use `https://`, or `http://` on `127.0.0.1` or `localhost`.
The validator refuses all other forms.

`McpClient` keeps the SDK connection open in one background task (`_hold`) from `start()` until `close()`. The SDK
cancel scopes must close in the task that opened them, so one task holds them. If the server stops during a call,
`call_tool` fails at once with the reason. It does not wait for the timeout. The stderr of a local server goes to
the debug log of north (`_StderrLog`).

## How north uses it

- `orchestrator/app.py` makes `McpManager(workspace_root=None)` and stores it on `deps.mcp_manager`. After north
  starts to serve, `_serve_mcp` calls `start_all` and then `register_tools`. At shutdown it calls `close_all`.
- `tools/specialized/mcp_tool.py` wraps each `McpToolDefinition` in an `McpTool` named
  `mcp__<server>__<tool>`. The tool is a change (`is_mutating`) unless the server sets `readOnlyHint`.
- `tools/learned_remote.py` uses `McpClient` and `McpServerConfig` to run a tool that north wrote for itself in its
  own process, and calls it over MCP.
- If a server does not start, `McpManager` closes it and calls `ToolRegistry.note_unavailable` with the key
  `mcp:<name>` and a reason that the user can act on (`_why`).
- `__init__.py` has no `__all__`. Callers import from `mcp_client.client`, `mcp_client.models` and
  `mcp_client.manager`.

## Config files

`McpManager.find_config_files` reads `<NORTH_HOME>/mcp.json` first. If a `workspace_root` is given, it also reads
`<workspace>/.north/mcp.json` and `<workspace>/mcp.json`. A later file replaces a server with the same name.
A server with `"disabled": true` is skipped. The live server passes `workspace_root=None`, so north reads only the
file in its home. A file in a cloned repository could name any command to run.

## Rules

- Keep the protocol in the SDK. Do not write JSON-RPC by hand here. If you raise the `mcp` pin, run the tests in
  `tests/unit/mcp_client` first, because 2.x renamed core names between minor releases (`pyproject.toml`).
- Give a local server only `agent_environment(network=True)` plus its own `env` block, never the environment of
  north. The environment of north holds keys such as `NORTH_ANTHROPIC_API_KEY`.
- Start servers in parallel and after north serves. One server that does not answer must not hold up startup.
- Catch the failure of each server inside `_start`. `start_all` calls `asyncio.gather` without
  `return_exceptions=True` and depends on this catch (compare CODING_STYLE §10.5).
- Send the version from `utils.version.NORTH_VERSION` in `client_info`. Do not write a version number here
  (CODING_STYLE §21.5).
- A tool states facts (`readOnlyHint`). The approval layer decides if a call asks (CODING_STYLE §7.3, §7.5).
- Wrap SDK and transport errors in `McpError` at this boundary (CODING_STYLE §13.4).
