# Coding agents: north delegates coding to Claude Code and Codex

> **Status:** accepted 2026-10-02, implemented on `feature/coding-agents-delegate-to-vendor-clis`.
> Evidence comes from a lab run against `claude` 2.1.286 and `codex` 0.159.2, and from
[`experiments/coding_agents_integration`](../../experiments/coding_agents_integration/README.md),
which drives north's real classes.

## Problem

north kept rebuilding what coding agents already do: an agent loop, edit tools, a sandbox,
session storage. Claude Code and Codex improve every few weeks; an in-house coder only falls
behind. north's value is elsewhere: memory, approvals, scheduling, audit.

## Decision

north does not write code itself. A `coding_agent` tool runs the user's locally installed
`claude` or `codex` in an isolated git worktree. The vendor owns the agent loop and the sandbox.
north owns four things:

1. **Policy** - every request the agent makes goes through `Approvals.decide()`.
2. **Isolation and proof** - a worktree per run, north runs the tests itself, then applies back.
3. **Handoff** - skills, context and memory go in; schedules stay north's (north starts runs).
4. **Audit** - run record, events, cost, ledger.

Closed decisions: code lives in a new `coding_agents/` module; the worktree manager is injected
from the composition root; auth is the user's own CLI logins; autonomous mode decides from
memory only (see Policy).

## Components

```
coding_agents/
  models.py      Mode, ToolRequest, RunSpec, RunEvent, RunOutcome
  base.py        CodingBackend: probe(), start(), events(), cancel(), resume()
  claude.py      headless stream-json backend
  codex.py       app-server JSON-RPC backend
  gate.py        ToolRequest -> approval Action -> Approvals.decide()   (the only caller of Approvals)
  hook.py        the fail-closed PreToolUse shim for Claude
  runner.py      worktree -> run -> verify -> apply-back, state machine, recovery
tools/specialized/coding_agent.py   the Tool agents call; describe() makes the start card
```

- Registered in `orchestrator/app.py` like `BashTool` (constructor args, so manual).
- A run is a child row in `agent_runs` and its events go to `agent_run_events`. No new tables.
  `provider_state` is `{provider: [entries]}` and append-only, so a run records
  `{"provider": "claude_code", "session_id", "worktree", "pid", "cli_version"}` and the latest
  entry wins. `AgentRunStore.set_status` (added in phase 0) moves a live run between `running`,
  `waiting_for_approval` and `interrupted`; `start()` again resumes an interrupted one.
- Worktrees reuse `GitWorktreeManager` and the workspace lock, passed in as a small interface.

## Backends

### Claude Code

`claude -p` with `--output-format stream-json --verbose`, stdin closed, a north-generated
`--session-id` saved before the process starts, `--resume` for follow-ups and recovery.

| Flag | Why (lab result) |
|---|---|
| no `--allowedTools`, `--permission-prompts none` | A broken hook must mean deny. Hooks fail open on timeout, exit 1, garbage output, a missing command, a dead HTTP hook. Only exit 2 or an explicit deny blocks. With default-deny, a broken hook was blocked and a working "allow" hook ran. |
| `PreToolUse` command hook via `--settings`, matcher on mutating tools | The hook sees Write, Bash, Agent and a subagent's calls. A 20s wait for a human worked. A deny rule beats a hook allow. |
| `--setting-sources user` | Default settings let a hostile repo run its own SessionStart hook and `.mcp.json` server and allow Bash. This stops all of it but drops `CLAUDE.md`, so north reads it and adds it to the prompt as untrusted guidance. |
| `--strict-mcp-config --mcp-config <north>` | Only north's servers. |
| sandbox on, `allowUnsandboxedCommands:false`, `failIfUnavailable:true`, `network.strictAllowlist`, `filesystem.denyRead` for north's and credential dirs | The vendor sandbox blocked an outside write and a non-allowlisted host. It covers shell only; file tools, WebFetch, hooks and MCP run outside it, so the gate still matters. |
| `--max-budget-usd`, `--max-turns` | Quotas. |

The shim exits 2 on any error and posts to the daemon with a per-run token, never north's secret.

### Codex

`codex app-server` over stdio. `initialize`, `thread/start` with `approvalPolicy: on-request` and
`sandbox: workspace-write`. North answers `item/commandExecution/requestApproval`,
`item/fileChange/requestApproval` and `item/permissions/requestApproval` with `accept`, `decline`
(the turn continues) or `cancel` (stop). Any other server request gets a JSON-RPC error, which
Codex treats as a refusal. `turn/interrupt` cancels; `thread/resume` recovers.

- Lab: inside write runs with no card; outside write asks and decline blocks; network is
  blocked silently. The sandbox is the boundary and the gate sees only escape attempts.
- `untrusted` asks for every command and patch (including `sort -o`, `git diff --output`,
  `find -exec`); kept as a strict option.
- `on-request` with an external sandbox asked nothing. Never use that pair.
- `codex app-server generate-json-schema` at probe time checks the methods north needs; a
  missing one marks the backend unavailable instead of guessing.
- **Secrets:** under plain `workspace-write` an agent can read north's `secret.key` and the
  Codex login token (lab: readable). A permission profile fixes it:
  `-c 'default_permissions="northworker"' -c 'permissions={northworker={extends=":workspace", filesystem={"~/.north"="deny"}}}'`.
  A path built at runtime was unreadable under the profile and readable without it, so the
  sandbox enforces it. Claude gets the same through `sandbox.filesystem.denyRead`. North passes
  both on every run, plus `~/.ssh`, `~/.aws`, `~/.gnupg`; whether `thread/start`'s `config`
  param accepts the profile is still to be checked.
- Codex's own sandbox inside another sandbox fails silently (the model said DONE, nothing was
  written). One sandbox only: the vendor's.

## Policy

The gate builds an `Action` from facts (kind, command, path, workspace, read_only, mutating) and
calls `Approvals.decide()`, passing the **owning task id** (the card is blocking and names it, which
frees the concurrency slot and keeps the watchdog away). The workspace comes from the server-owned
run record, never from the hook payload's `cwd`. Nothing else decides (CODING_STYLE 7.3).

- Reads inside the worktree: allowed, no card.
- Edits: policy decides. The existing rule "edit inside the task workspace" already allows them in
  safe and autonomous; ask mode asks. The card shows a diff.
- Shell commands: bash's existing command classifier decides.
- Anything else: ask, or deny when nobody can be asked (the denial is recorded, not lost).
- **Autonomous decides from memory only.** An action that leaves the sandbox or the workspace
  (a push, a new host) is allowed only when a fact the user stated covers it. With no covering
  fact the model abstains and a card waits. Facts written by a worker, a repo or a tool never
  authorize anything. YOLO stays always-yes. Autonomous has no hard floor today, so this lives in the
  decider (its prompt, plus a new `Action.leaves_sandbox` fact it keys on), not in the policy.

## Lifecycle

```
queued -> starting -> running <-> waiting_for_approval -> verifying -> applying -> done
                          \-> needs_attention | failed | cancelled
```

1. Create the worktree, write the run record (with the session id), then start the process. The
   tool first looks for a live coding run for the same task id, so a re-planned task that was
   resumed continues the session instead of starting a second one.
2. Daemon restart: north's recovery resumes a task killed mid-run (no side effect is recorded until
   a mutating call succeeds) and fails one killed after apply-back, with a note. A run whose process
   is dead resumes with its session id, up to `MAX_RESUME_ATTEMPTS`. If resume fails the job goes to `needs_attention`; it never silently
   starts fresh. Only processes north positively identifies as its own are ever killed.
3. Rate limit, auth and billing errors freeze the run and resume it later (CODING_STYLE 13.5).
4. After the agent ends, north runs the repo's verify command in the worktree and checks the
   agent's claims against what succeeded. "DONE" is never trusted.
5. Apply-back is automatic only when verify passed and there is no conflict; otherwise the
   branch is kept.

## Waits on the dashboard

Every wait must be visible. Proved by the experiments, or marked to build:

| Wait | Shown | State |
|---|---|---|
| Approval card (gate or start) | Dashboard `attention`, Approvals page | works |
| Slot held by a waiting task | not held | works |
| Run waiting for approval | run row says `waiting_for_approval` | state done; the gate sets it in phase 1 |
| Job parked for a person | dashboard `jobs`, `needs_attention` | works |
| Vendor rate limit or auth freeze | run event plus a paused state | build |
| Apply-back conflict, branch kept | a non-blocking card | build |
| Queued behind the concurrency cap | Tasks page "Queued" | works |

## What north sends the agent

| north has | How it reaches the agent |
|---|---|
| Skills | Already `SKILL.md` folders; Claude loads them from a directory flag. |
| Context | A prompt file chosen for the task (north's engineering-domain scoping applies). |
| Memory | A read-only loopback MCP server with a per-run token, tool list filtered by policy. |
| Schedules | Stay north's: north starts the run. |

## Proof

1. Conformance tests with a fake `claude` and a fake `codex` driven by recorded streams: every
   hook failure denies, `on-request` + external sandbox is rejected, a hostile repo's settings
   do not load, resume works, an unknown request is refused.
2. An opt-in live canary per installed CLI (one tiny task); records the CLI version.
3. `evals/` coding tasks run through the old coder and the new tool; the old coder is deleted in
   a follow-up PR only if the new path matches or beats it.

## Phases

0. **Done.** Close the four gaps the experiments found, each with its tests: `Action.leaves_sandbox`
   (set by `git push` and mutating `gh` calls), run `set_status`, and the decider's
   abstain-without-a-fact rule. Episodes never cover an action.
1. Claude `plan` mode: tool, run store, probe, events. No writes.
2. Claude `edit` mode: worktree, gate, verify, apply-back, autonomous abstain rule.
3. Codex backend on the same conformance suite.
4. Recovery and freeze.
5. North MCP recall server; cross-review as a flow.
6. Follow-up PR: delete `coder`, `architect`, `reviewer` and the edit tools.

## Open risks

- Connection to cards, slots, dashboard, runs API and recovery is proved by
  `experiments/coding_agents_integration` (20 pass, 4 expected gaps). Still unproven: a real
  Claude Code process against the daemon, which is phase 1.
- `~/.claude.json` is writable by the CLI; an agent could add an MCP server there.
- North and the Codex worker share one ChatGPT plan quota.
- Subscription terms are the vendors' call and can change.
- A recalled fact carries no origin; a fact extracted from a task's output could cover a push. Tracking
  where a fact came from is a follow-up.
