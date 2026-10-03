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
  `{"provider": "claude_code", "session_id", "worktree", "pid", "cli_version"}` as entries; read them
  merged in order, the latest value of each key wins. `AgentRunStore.set_status` (added in phase 0) moves a live run between `running`,
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

### Codex, as built

`codex app-server` over stdio, speaking JSON-RPC (`coding_agents/appserver.py`, `coding_agents/codex.py`).
What the lab measured, and what the code does because of it:

- **No `sandbox` in `thread/start`.** An explicit sandbox overrides the permissions profile, and the
  deny-read then silently does nothing (files were readable). The profile goes in `thread/start`'s
  `config` instead, with no launch flags: `{"default_permissions": "northworker", "permissions":
  {"northworker": {"extends": ":workspace", "filesystem": {"~/.north": "deny", ...}}}}`, plus
  `approvalPolicy: on-request`. A path built at runtime was unreadable under it and readable without.
- **Plan mode is the `:read-only` profile.** Every write the agent attempts becomes an approval
  request, and plan mode declines them all; they are listed as refused. **Edit mode is `:workspace`:**
  an edit inside the copy needs no approval, and anything the sandbox refuses arrives as a request.
- **Approvals go to the gate, over the route and token the Claude hook uses**, as hook-shaped payloads
  (Bash for a command; Write per file for a file change). The file-change request names only an item id,
  so north reads the files from the item announced just before it. Only an explicit ALLOW is accepted: a
  pass means "the agent's own rules decide", and a Codex request is the agent saying its own rules need
  an answer, so a pass, a denial and an unreachable gate all decline. An unknown kind of request gets an
  error, which Codex reads as a refusal.
- **The session is Codex's thread id**, not an id north makes up. The backend reports it as soon as it
  has one and the runner saves it, so `thread/resume` (which works in a new process) asks for a thread
  that exists. A thread that is gone is reported as lost, never started over.
- **The probe checks the protocol**, not just the version: `codex app-server generate-json-schema` must
  still list the methods north uses, or the backend is unavailable with a reason.
- Codex reports tokens but no dollar cost, so the run records tokens. It has no turn or budget cap to
  pass, so only the time limit bounds a Codex run. A failure is an `error` notification and then a
  failed turn; `codexErrorInfo` and the nested HTTP status say whether it is a limit, a login, a bad
  setting or a resource that will come back.
- **The agent's copies live outside the temp directory** (`~/.cache/north/coding-worktrees`). Both
  agents' sandboxes let a command write to temp, so a copy there could be changed by another run's
  commands; in the cache directory a run's sandbox can write only to its own. Found when a live test's
  "outside" folder, in temp, was writable by design.
- `untrusted` asks for every command and patch (including `sort -o`, `git diff --output`,
  `find -exec`); kept in mind as a strict option. `on-request` with an external sandbox asked
  nothing, and Codex's own sandbox inside another fails silently: one sandbox only, the vendor's.

Live, with a real `codex`: a plan answers from the repo and changes nothing; a thread resumes in a new
process with what it read; an edit lands on a branch with the real tree clean; a write outside the copy
reaches the gate and is refused; north's own directory is unreadable; and a change that passes north's
own test run lands in the working tree.

### Plan mode, as built

`--permission-mode plan` with nothing pre-allowed and `--permission-prompts none` needs no hook: in
the lab the Read tool outside the working directory was refused and shell `cat` outside it was
denied, and every refusal is listed in the result and shown to the model. The hook arrives with
edit mode. `claude` older than 2.1.259 is reported unavailable, since the flags above
need it. A worker starts with only `HOME`, `PATH` and a few locale variables: no `NORTH_*` secrets
and no other provider's key.

### Edit mode, as built

An edit run works in an isolated git copy (`orchestrator/worktree.py`) on a throwaway branch and leaves
the result on that branch; **nothing is applied to the real working tree** (that is the next PR). The
agent runs in the default permission mode with nothing pre-allowed, so every action that changes
something needs a decision, and the decision comes from a `PreToolUse` command hook
(`coding_agents/hook.py`, standard library only) that posts to the daemon's loopback route
(`orchestrator/api/coding_gate.py`). The route is not behind the API secret: it takes a per-run token
that names one run and stops working when the run ends.

The gate answers one of three things. ALLOW and DENY are explicit. PASS says nothing, so the agent's
own rules decide; north passes only a single, plainly read-only shell command (`ls`, `git status`, ...),
so a pass can never grant anything. Whatever is left goes to `Approvals.decide()` as an `Action`, so a
coding agent is held to the same policy, memory and modes as any tool (`orchestrator/coding_gate.py`).
A run waiting on a card shows `waiting_for_approval` on the dashboard, and the card names the task so
the slot is freed.

What the lab and the live tests (`tests/live`) measured, and the code now does:

- `autoAllowBashIfSandboxed` is **off**. On, a broken hook let a sandboxed command run; off, the same
  broken hook blocks it. The hook has a day-long timeout and exits 2 on any failure, the only exit
  code that blocks.
- The agent's file tools are not sandboxed, and an allow from the hook overrides their own check: with
  an allow-all hook they wrote `~/x` and `../x`. The gate therefore resolves every file path itself
  (relative to the run's own copy, following `..` and symlinks) and asks about, rather than allows,
  anything outside it. Git's own files, including the `.git` pointer a linked copy has, are never
  changed, in any mode.
- An edit inside the run's copy asks nobody (`Action.isolated`): nothing real changes until a person
  lands it, and the start card already covered it. A shell command is ruled on by the mode like any
  other, and a wrong ALLOW on a write outside the copy was still stopped by the kernel sandbox.
- Live, with a real `claude`: an edit lands on a branch while the real tree stays clean; a declined
  command is denied while edits in the copy needed no card; absolute, `../` and traversal writes
  outside the copy are refused; overwriting `.git` is refused even in YOLO; and with the gate
  unreachable every change is blocked and listed as refused.

### Landing, as built

When an edit run finishes well and changed something, north does two things the agent cannot do for
itself. **It runs the project's own tests**, in the agent's copy, through north's existing `bash` tool,
so the command is ruled on by the approval layer and runs under the OS sandbox like any command north
runs. The command comes from the project's markers (`detect_verify_command`), never from what the agent
wrote, and is found in the real repository because that is where its virtualenv lives; `node_modules`
is linked into the copy only while the tests run. A passing run, a failing run, a missing test runner
(read from `No module named ...`, which exits 1 like a failing suite), a declined run and a project
with no tests are five different results, and none of the last three is reported as a failure.

**It offers the change to the user** (`orchestrator/coding_landing.py`), through `Approvals.decide()`,
as one question per change: your mode and memory decide, so ask mode asks and autonomous reads your
facts. A change whose tests failed is not offered at all. Agreed, it is applied under the workspace
lock as **uncommitted changes in the working tree**, your history untouched, and the copy and branch
are removed. Declined, refused, or in conflict (the same lines changed there meanwhile, so the apply
is refused and nothing is overwritten), the copy is removed and the branch stays in your repository.

Proved with the real `claude` (`tests/live`): a change that passes north's own run lands in the working
tree; a change the agent broke on purpose, while it claims all tests pass, is not applied, and the
real tree stays untouched.

Known limits: the copy has no network, and the repo's installed dependencies (a virtualenv,
`node_modules`) are not in it, so the agent often cannot run the tests. That is why north runs the
verification itself.

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
| Vendor rate limit or auth freeze | run event plus a `paused` run | works |
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
1. **Done.** Claude `plan` mode: the `coding_agents` module, the `coding_agent` tool, the run store
   adapter, probe and events. No writes. Proved against the real `claude` by `tests/live`
   (`NORTH_LIVE_CLAUDE=1`): a plan answers from the repo and leaves it untouched, and a session
   resumes with what it already read. The module sits in the `intelligence` layer so `tools` may
   import it; the run store and, later, the approval gate reach it through protocols, wired in
   `orchestrator/app.py`.
2. Claude `edit` mode, in two PRs.
   - **2a. Done.** Worktree, the gate (hook, loopback route, judge), the approval mapping, and the
     result left on a branch. Nothing applied.
   - **2b. Done.** North runs the repo's tests itself, offers the change through the approval layer,
     and applies it under the workspace lock (keeping the branch on a conflict). The optional
     cross-review by the other agent waits for the Codex backend: with only Claude there is no
     "other agent" to ask.
3. **Done.** Codex backend (app-server, a permissions profile, approvals through the gate), with the
   same tests as Claude's.
   - **Cross-review. Done.** After the tests pass, the other agent reads the diff, read-only, in a
     fresh plan run. The diff goes in the prompt inside a fence it cannot close. The verdict
     (`VERDICT: OK|CONCERNS`; no readable verdict is UNCLEAR, never a pass) rides on the landing
     card, labelled an opinion. It is advice: it never blocks or allows a landing. Skipped when the
     tests failed, with one agent, or with `review: false`.
4. **Done.** Recovery and freeze. A usage limit, an outage or a logged-out agent PAUSES the run
   (status `paused`, reason kept, session and copy kept) instead of failing it; the tool tells the model
   to call again with the same task, which resumes the session. At startup, coding runs that say
   `running` or `waiting_for_approval` but whose process is gone are marked `interrupted`, so the
   dashboard stays truthful and the task's own recovery resumes them. A paused run whose copy was
   deleted is closed as failed rather than left paused for ever. North does not re-call the agent by
   itself: the agent loop turns a tool's errors into results, so the model (or the person) decides
   when to try again.
5. **Done, as a briefing instead of a memory server.** At the start of a run north puts the user's
   relevant facts, profile and up to two matching skills in the agent's guidance (episodes are left
   out). A memory MCP server was dropped: the agent does not need to ask for what it can be told, and
   a second door into memory is a second place to be misled. The block is marked as background that
   never widens what the agent may do; only the approval layer authorizes. A failing briefing never
   stops a run.
6. Follow-up PR: delete `coder`, `architect`, `reviewer` and the edit tools.

## Open risks

- Connection to cards, slots, dashboard, runs API and recovery is proved by
  `experiments/coding_agents_integration`, and a real `claude` against the real route and approval layer
  by `tests/live`. Still unproven: a whole task chosen and run by north's own planner.
- A run waiting on a card holds its `claude` process open, and a daemon restart ends it; recovery
  resumes the session in the same copy (phase 4, done).
- Shell commands in an edit run are ruled on one at a time; under the ask mode a long task means many
  cards, and under autonomous each goes to the memory decider.
- `~/.claude.json` is writable by the CLI; an agent could add an MCP server there.
- North and the Codex worker share one ChatGPT plan quota.
- Subscription terms are the vendors' call and can change.
- A recalled fact carries no origin; a fact extracted from a task's output could cover a push. Tracking
  where a fact came from is a follow-up.
