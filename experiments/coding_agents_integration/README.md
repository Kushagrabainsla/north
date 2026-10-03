# Coding-agent integration experiments

Date: 2026-10-02

## Question

Can a coding agent's requests, waits and runs connect cleanly to north's real cards,
queue, dashboard and recovery as they are today - before the feature is built?

Each test is one atomic question, run against production classes (`Approvals`,
`ApprovalPolicy`, `ApprovalStore`, `AgentRunStore`, `ToolRegistry`, `MemoryDecider`,
`recover_interrupted_tasks`, and the real app through its real lifespan). The only
stand-ins are the pieces the design adds: the Claude hook shim, its loopback route,
and a tool shaped like `coding_agent` (`prototypes.py`).

## Decision

What these results fix in [`docs/design/coding-agents.md`](../../docs/design/coding-agents.md):

- A gate card must carry the **owning task id** and be blocking. That alone frees the
  concurrency slot and keeps the stuck-task watchdog away (a card with no task id freed nothing).
- The gate takes the **workspace from the server-owned run record**, never from the hook
  payload's `cwd`.
- The "memory only, abstain without a fact" rule is a **decider change**, not a policy
  floor: autonomous has none today, by design.
- A coding run is a child row in `agent_runs`. Its state lives in `provider_state`, which is
  `{provider: [entries]}` and append-only, so the latest entry under `claude_code` / `codex` wins.
- The hook shim is a command that exits 2 on any failure; one gate call costs about 37 ms.
- The `coding_agent` tool looks up a live run by task id so a re-planned, resumed task
  continues the session instead of starting a second one.

## Evidence

| # | Question | Result |
|---|---|---|
| G1 | Does a gate request become a blocking card owned by the task? | yes |
| G2 | What does each mode do with each request a coding agent makes? | table in [`results.json`](results.json); reads never ask, writes ask in ask/safe, an abstaining decider waits, yolo allows |
| G3 | Does autonomous have a hard floor today? | no: an approving decider lets a push through (pinned) |
| T1 | Does a `coding_agent`-shaped tool fit `ToolRegistry` + approvals? | yes: start card names the task, runs only after approval, reject returns `refused` |
| S1 | Does a waiting gate card free the concurrency slot? | yes, only when it names the task |
| S2 | Does a pending gate card show on the dashboard? | yes: `attention` lists it |
| S3 | Is a coding run readable through the runs API? | yes, with the `provider_state` shape above |
| S4 | Does a job parked for attention show on the dashboard? | yes: `needs_attention` |
| H1-H3 | Does the shim round-trip allow, deny and a slow human? | yes (0, allow/deny JSON); a 2 s wait is answered |
| H4-H6 | Does the shim fail closed? | server down, unknown token and garbage answer all exit 2 with no stdout |
| H7 | Gate call cost | median 37 ms, p95 38 ms (15 runs, read-only request) |
| R1 | Does recovery resume a task killed mid-run? | yes: no side effect is recorded until a mutating call succeeds |
| R1b | Does it re-run a task killed after a side effect? | no: failed with a note, which is the right rule after apply-back |
| R2 | Can a resumed task find its live coding run? | yes, by task id and agent prefix |

## Gaps the build had to close

The first run had four `xfail(strict=True)` tests that asserted what the design needs.
Phase 0 closed all four, with their tests now in `tests/unit/`, and the experiments pass
without any expected failure:

1. `Action.leaves_sandbox` is a fact the decider keys on. `git push` and mutating `gh`
   calls set it.
2. `AgentRunStore.set_status` moves a live run between `running`, `waiting_for_approval`
   and `interrupted`, and refuses to bring back a finished one.
3. A dead run is `interrupted` and `start()` again resumes it (no separate method needed).
4. The decider approves an action that leaves the sandbox only when the reply cites a
   fact, past decision, rule or profile item. Citing nothing, or only an episode, makes
   the card wait.

## Waits on the dashboard

| Wait | Shown today | State |
|---|---|---|
| Approval card (gate or start card) | Dashboard `attention`, Approvals page | works (S2) |
| Slot held by a waiting task | not held | works (S1) |
| Run waiting for approval | run row says `waiting_for_approval` (the gate sets it in phase 1) | state works |
| Job parked for a person | dashboard `jobs` as `needs_attention` | works (S4) |
| Vendor rate limit or auth freeze | nothing yet | to build: run event plus a visible paused state |
| Apply-back conflict, branch kept | nothing yet | to build: a non-blocking card (`waiting_for_you`) |
| Queued behind the concurrency cap | Tasks page "Queued" | works today |

## Reproduce

```bash
.venv/bin/python -m pytest experiments/coding_agents_integration -q
```

## Limitations

- The shim talks to a loopback prototype, not to real Claude Code; the real-CLI behaviour
  (fail-open hooks, default-deny) was measured separately in the lab recorded in the design doc.
- Deterministic checks. The dashboard is exercised through its API handlers, not rendered.
- The decider uses a stub model and memory; whether a real model abstains is phase 2.
- A recalled fact carries no "who wrote it". Episodes never cover an action, but a fact extracted
  from a task's output could; tracking a fact's origin is a follow-up.
- Codex's side (app-server requests) is not exercised here; it reuses the same `Approvals` path.
- A long wait is simulated in seconds; the "cards never expire" rule is already covered by
  `tests/unit/approval/test_cards_never_expire.py`.
