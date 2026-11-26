# tools

The `tools` package holds every tool that north's agents can call, and the layer that runs each call.
A tool is a `Tool` subclass with a `name`, a `description`, a JSON Schema for its parameters and an async `run()`.
`ToolRegistry` finds the tools on disk, gives each tool the approval layer, and keeps one global catalog.
The agent loop picks a small set of tools from that catalog for each task and calls them through `Tool.execute`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `Tool` (ABC), `prepared` | The tool interface. `execute()` checks, decides and runs each call. Class flags state facts about the tool: `is_mutating`, `uses_secrets`, `locks_workspace_itself`, `starts_runs`, `model_reads_formatted`, `view_markers`. |
| `models.py` | `ToolInput`, `ToolOutput`, `ConfidenceScore` | Pydantic models for one call. `ToolInput` has server-owned fields that the model cannot set (`granted_workspace`, `edit_scope`, `approved`, `user_request`). `ToolOutput.failure_kind` is `error`, `not_found` or `refused`. |
| `registry.py` | `ToolRegistry`, `LearnedTool`, `written_by_north`, `is_learned_tool` | Finds tools in `universal/`, `specialized/` and north's learned folder. Scans again at most every 2 seconds. `require()` hides a tool while the machine lacks what it needs. `ask_ahead()` decides several calls on one card. |
| `exceptions.py` | `ToolError`, `ToolNotFoundError`, `ToolExecutionError` | Tool-layer errors. `ToolNotFoundError` also subclasses `utils.tools.ToolNotFoundError`. |
| `_schema.py` | `conform_params` | Checks call arguments against the declared schema. Converts a value only when its meaning cannot change ("120" to 120). |
| `_path.py` | `resolve_path`, `target_path`, `is_sensitive_path`, `personal_dir`, `edited_paths`, `named_paths` | The fail-closed path gate. Blocks credential folders and north's home, except task folders and personal data. Also finds the paths that a shell command names or edits. |
| `_diff.py` | `unified_diff` | A diff with a size limit, for a file-write card. |
| `confidence.py` | `ConfidenceTracker`, `RELIABLE_TOOLS` | SQLite scores per (agent, tool), updated as a moving average. Also keeps model scores and speeds for `inference/dispatcher.py`. |
| `retrieval.py`, `tool_index.py` | `tool_retrieval_profile`, `bm25_rank`, `reciprocal_rank_fusion`, `ToolIndex` | Tool search: an embedding index and BM25, fused by rank. |
| `output_spill.py` | `OutputSpillStore`, `store_overflow`, `overflow_note` | Keeps the full text of a result that was too long for the model. The store has a limit, so a handle can expire. |
| `learned_env.py` | `ToolEnvironments`, `script_dependencies`, `pin_problems` | Makes one `uv` environment per learned tool, from its PEP 723 `# /// script` block. Each package must be pinned to one version. |
| `learned_remote.py` | `RunnerTool`, `LearnedRuntime` | A learned tool as north sees it. Its code runs in its own process, under the OS sandbox, through north's MCP client. The process starts on the first call and stops after `IDLE_SECONDS`. |
| `learned_runner.py` | (script) | Runs in the tool's own environment and serves the tool over MCP on stdio (`describe`, `run`). It imports nothing from north. It defines small stand-ins for `Tool`, `ToolInput`, `ToolOutput` and `secret`. |

### `universal/`

General tools. Files that start with `_` are helpers.

- Files and web: `write_file.py`, `fetch_url.py`, `web_search.py`, `read_tool_output.py`.
- Browser: `browser.py` (`BrowserTool`). It uses chrome-agent for managed sessions and `browser_taskwindow.py` (`TaskWindowBrowser`) for your existing Chrome profiles. Helpers: `browser_describe.py` (facts for the card), `browser_query.py`, `browser_deep.py`, `browser_accounts.py` (`BrowserAccounts`).
- Self-extension: `create_tool.py`, `create_skill.py`, `create_flow.py`, `create_view.py`. Helpers: `_draft_checks.py`, `_procedure_edit.py`.
- Skills and flows: `use_skill.py`, `use_flow.py`, `run_flow.py`. `flow_runner.py` (`FlowRunner`), `flow_schedules.py` (`FlowSchedules`) and `_schedules.py` hold no tool. They run and schedule flows.
- Memory and tasks: `remember.py`, `forget.py`, `offered_before.py`, `update_plan.py`, `get_task_status.py`, `get_active_sessions.py`, `query_metrics.py`.
- Devices: `take_screenshot.py`, `take_photo.py`.

### `specialized/`

Integration and execution tools.

- `bash.py` (`BashTool`): shell commands. Sandbox helpers: `_os_sandbox.py` (`current()`), `_seatbelt.py` (macOS Seatbelt profiles), `_egress.py` (the one network proxy for sandboxed commands), `_sandbox.py` (Docker).
- `coding_agent.py` (`CodingAgentTool`): gives a coding task to the installed Claude Code or Codex. `coding_shell.py` (`BashShell`) runs the coding runner's test commands through `bash`.
- `gh.py` (`GhTool`), with `_subprocess.py`.
- `mcp_tool.py` (`McpTool`): one tool of an MCP server.
- `north_config.py` (`NorthConfigTool`), `screen_brightness.py` (`ScreenBrightnessTool`, with `_display.py` and `_mac_display.py`).
- `fitness_api.py`, `interview_prep.py`.

## How a call reaches the approval layer

`ToolRegistry._bind()` gives each registered tool the `Approvals` object. Then `Tool.execute()` does these steps for each call:

1. It releases only the secrets in `uses_secrets` (`config.secret_store.released_to`).
2. `prepare()` checks the arguments with `conform_params`. If an argument is wrong, the call fails and the model sees what the tool takes.
3. `prepare()` calls `unavailable()`. If the result is not "", the call fails with that reason and no card shows.
4. If `mutates(params)` is false, the call runs. Only the paths from `reads()` are checked against an earlier "no" in the task (`Approvals.refused_paths`).
5. If the call mutates, `describe()` returns a `Request`. If it returns `None`, the call runs with no card.
6. Otherwise `Approvals.decide()` rules. A refusal returns `ToolOutput(failure_kind="refused")`. An allowed call runs with `input.approved` set, and `run()` reads it with `prepared(input)`.
7. `_change()` holds `workspace_lock` for the target folder during `run()` only, unless `locks_workspace_itself` or `starts_runs` is set.
8. `execute()` removes every stored secret value from the data, the error and any exception text.

`ToolRegistry.ask_ahead()` uses the same `prepare()`, so the call decided ahead is the call that runs. `execute_for_you()` runs a call that you make on a dashboard panel (`web/views.py`), with no card.

## How north uses it

- `orchestrator/app.py` (`_build_tool_registry`) makes `ToolRegistry(auto_register=True, learned_dir=..., approvals=...)`. It registers by hand each tool that needs constructor arguments. These are `BrowserTool`, `CreateToolTool`, `BashTool`, `CodingAgentTool`, the memory and task tools, and later `UseSkillTool`, `CreateSkillTool`, `UseFlowTool`, `RunFlowTool` and `CreateViewTool`. It calls `require()` for `take_screenshot`, `take_photo` and `browser`.
- `orchestrator/app.py` (`_build_tool_index`) makes the `ToolIndex`. `config/dependencies.py` makes the `ConfidenceTracker`.
- `agents/agentic_llm_agent.py` is the agent loop. It calls `available_tools()`, `execute()`, `ask_ahead()`, `task_note()`, `before_task_ends()` and `ConfidenceTracker.record_use()`. It runs read-only calls together and mutating calls one at a time.
- `agents/base.py` selects the tools for a task from `available_tools()`. `agents/context_compaction.py` uses `output_spill`.
- `orchestrator/orchestrator.py` calls `ToolRegistry.end_task()` when a task ends.
- `mcp_client/manager.py` registers one `McpTool` per server tool.
- `web/extensions.py` (dashboard tools and flows), `web/views.py` (`execute_for_you`) and `web/secrets_panel.py` (`written_by_north`) use the registry.
- `approval/unattended.py`, `context/repo_map.py` and `web/artifacts.py` use `_path`.
- Public interface (`__all__` in `__init__.py`): `Tool`, `ToolRegistry`, `ToolInput`, `ToolOutput`, `ConfidenceScore`, `ConfidenceTracker`, the confidence constants and the three exceptions. Most callers import from the submodules.

## Rules

- To add a tool, add a file with a `Tool` subclass to `universal/` or `specialized/` (CODING_STYLE §6.5). Discovery skips files that start with `_`. If the tool needs constructor arguments, register it in `orchestrator/app.py`.
- A tool describes; the approval layer decides (CODING_STYLE §7.3). Override `describe()` to put facts on an `Action`. Do not read the approval mode, keep an allowlist or remember answers.
- Put why a call cannot run in `unavailable()`, not in `describe()` (CODING_STYLE §7.5). If a whole machine lacks something, use `ToolRegistry.require()`.
- If a call can change state, set `is_mutating = True` or override `mutates()`. Only mutating calls go to the approval layer.
- Callers use `execute()`. Do not call `run()` for an agent, and do not override `execute()`.
- In `run()`, return `ToolOutput(success=False, ...)` for a recoverable error; do not raise. If the answer is "it is not there", set `failure_kind="not_found"`, so the tool keeps its confidence.
- Declare each secret in `uses_secrets` and read it with `secret(name)` inside `run()` (CODING_STYLE §7.6). The model knows only the name.
- Resolve every path through `resolve_path()` (CODING_STYLE §16.1.1). Take the write folder from `input.granted_workspace`, never from `params["workspace"]` alone.
- A tool north wrote is a change on every call (`LearnedTool`). The exception is a `RunnerTool` call that matches `read_only_when` while the OS sandbox is on (CODING_STYLE §7.3).
- Do not add a tool that wraps a shell command. Reads go through `bash` (CODING_STYLE §16.3).
- Put platform code behind one interface, such as `_os_sandbox.current()` or `_display.current()` (CODING_STYLE §16.4).

## Data on disk

All paths are in north's home (`settings.north_home`).

- `learned/tools/universal/` and `learned/tools/specialized/`: live learned tools, `<name>.py` and `<name>.json`. Drafts go in `learned/tools/candidates/`.
- `learned/envs/<tool>-<key>`: a learned tool's environment. `learned/data/<tool>`: its own folder.
- `tools.db`: confidence scores. `tool_index.db`: the tool search index. `browser/accounts.db`: browser profile accounts.
