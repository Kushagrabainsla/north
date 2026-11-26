# coding_agents

The `coding_agents` package hands coding work to the coding agents installed on this computer: Claude Code (`claude`) and Codex (`codex`). North does not write code. It runs the user's own agent in plan mode (read only) or in edit mode (changes in an isolated git copy). Then it records the run, tests the change and offers it to the user. The agent tool that uses this package is `coding_agent` in `tools/specialized/coding_agent.py`. The design notes are in `docs/design/coding-agents.md`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `runner.py` | `CodingRunner`, `RunReport`, `AGENT_PREFIX` | One run from start to end. Picks an agent that is ready, continues a live run of the same task, makes the copy, issues a gate token, runs the agent, then verifies, reviews and lands an edit. `unavailable()` says why no agent can take a run. |
| `base.py` | `CodingBackend` (ABC), `RunRecorder`, `Workspaces`, `Shell`, `Verifier`, `Lander`, `Briefing` (Protocols), `LiveRun`, `RunStart`, `ShellResult` | The interface of a backend, and the small protocols north implements and gives to the runner. |
| `models.py` | `Mode`, `RunSpec`, `RunEvent`, `RunOutcome`, `FailureKind`, `PAUSING_FAILURES`, `WorkTree`, `WorkChange`, `Verification`, `Review`, `Landing`, `GateAccess`, `AskAccess` | Value objects of a run. |
| `claude.py` | `ClaudeBackend` | Runs `claude -p` with stream-json output. Loads only the user's settings, turns on the vendor sandbox, and in edit mode adds the `PreToolUse` hook. |
| `codex.py`, `appserver.py` | `CodexBackend`, `AppServer` | Runs `codex app-server` over JSON-RPC on stdio. A permission profile confines the run. In edit mode each Codex approval request goes to the gate through `gate_client.ask_gate()`. |
| `confinement.py` | `codex_filesystem`, `profile` | The filesystem part of a Codex profile: the home folder is closed, and only what the run needs is open. |
| `gate.py` | `Gate`, `GateSessions`, `GateSession`, `Judge` (Protocol), `Decision`, `Verdict`, `ToolRequest`, `hook_response` | Rules on each tool call of an edit run. A plainly read-only command gets `PASS`. A write to git's own files gets `DENY`. The `Judge` decides the rest. |
| `hook.py` | (script) | The hook Claude Code runs before each tool call. It uses the standard library only, posts the call to north and prints the answer. Any problem exits 2, which blocks the call. |
| `gate_client.py` | `ask_gate` | The same gate question for Codex. Anything but an explicit allow is a refusal. |
| `ask.py` | `handle`, `Asker` (Protocol), `Question`, `Reply` | A minimal MCP server protocol for the `ask_north` tool (and `fetch_url` when a fetcher is set). It is the one way a coding agent asks north something. |
| `verify.py` | `CommandVerifier` | Runs the project's own test command in the copy through `Shell`. North does not trust the agent's own claim that tests pass. |
| `review.py` | `review_task`, `parse_review` | Asks a second agent, read only, to find problems in a diff. The verdict is advice; it does not land or block a change. |
| `discovery.py` | `discover_backends`, `check_agents`, `try_agents`, `Readiness` | Finds the installed agents. `try_agents()` also sends a one-line request to prove the account has credit. |
| `environment.py` | `agent_environment`, `in_project` | The environment a child process gets: only the listed names, and the project's virtualenv active. North's secrets stay behind. |
| `constants.py`, `process.py`, `exceptions.py` | turn and budget caps, timeouts, `process_identity`, `tail`, `CodingAgentError`, `BackendUnavailableError` | Small helpers. |

## How a run goes

1. `CodingRunner.run()` picks the backend and probes it. The run is recorded before the process starts.
2. In edit mode, `Workspaces.create()` makes a copy on a throwaway branch.
3. If the run edits or may ask north, `GateSessions.issue()` makes a token for it.
4. The backend runs the agent. Each event goes to the `RunRecorder`.
5. In edit mode, each tool call goes through the gate, and `Judge` sends the rest to the approval layer.
6. When the agent stops, the runner revokes the token and commits the copy's changes to its branch.
7. If the run ended well and changed files, `Verifier` runs the tests. If the tests did not fail and a review was asked for, a second agent reads the diff. Then `Lander` offers the change to the user.
8. A paused run keeps its session and its copy, so it can continue later.

## How north uses it

- `orchestrator/app.py` (`_register_coding_agent()`) calls `discover_backends()` and builds the `CodingRunner`. It also builds `GateSessions` and `Gate`, and registers `CodingAgentTool`.
- The orchestrator implements the protocols: `AgentRunRecorder` (`orchestrator/coding_run_recorder.py`), `GitWorkspaces` (`orchestrator/coding_workspaces.py`), `ApprovalJudge` (`orchestrator/coding_gate.py`), `ApprovalsAsker` (`orchestrator/coding_ask.py`), `ApprovalsFetcher` (`orchestrator/coding_fetch.py`), `LandingDesk` (`orchestrator/coding_landing.py`), `MemoryBriefing` (`orchestrator/coding_briefing.py`) and `BashShell` (`tools/specialized/coding_shell.py`).
- `orchestrator/api/coding_gate.py` serves `POST /orchestrator/coding/gate` for the hook. `orchestrator/api/coding_ask.py` serves `POST /orchestrator/coding/ask` for `ask_north`. Both check the run token, not the API secret.
- `orchestrator/nightly_check.py` calls `try_agents()`. `web/extensions.py` calls `check_agents()`.
- `mcp_client/client.py` and `tools/learned_remote.py` use `agent_environment()` for their own child processes.
- The public interface is the long `__all__` in `__init__.py`.

## Rules

- Do not build what Claude Code or Codex already do (agent loop, sandbox, edit tools, sessions). North owns only policy, the copy, verification, memory handoff and audit (CODING_STYLE §22).
- Do not import the approval layer or north's stores here. The composition root gives the runner implementations of the protocols in `base.py` (CODING_STYLE §6.3). Today the package imports only `utils` and `exceptions` from north.
- The approval decision stays in the approval layer. The gate settles only what is plainly safe or plainly wrong; `Judge` decides the rest (CODING_STYLE §7.3, §7.5).
- Fail closed. A broken hook, an unreachable gate or an unread reply is a refusal.
- `hook.py` must import nothing from north. It runs as its own process.
- A missing resource (credit, login, a pause) freezes a run; it does not fail it. `PAUSING_FAILURES` keeps the session and the copy (CODING_STYLE §13.5).
- An approval never times out (CODING_STYLE §13.5). `HOOK_TIMEOUT_SECONDS` is one day for this reason.
- Text from the repository or a diff is untrusted data. Hand it over fenced, never as instructions.
