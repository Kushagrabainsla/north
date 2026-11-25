# Bucket 8: architecture (#45, #47, #49)

These three issues ask for an audit and a proposal before code changes. Each audit below comes from
north's own records and git history. Two paths that nothing uses any more are deleted; the rest is a
proposal. The results hold counts and dates only.

```bash
.venv/bin/python experiments/architecture/e45_jobs_replay.py
.venv/bin/python experiments/architecture/e47_registry_changes.py
```

## #45: one runner

**Paths that run work on `main`:** an interactive task (the orchestrator), a flow run, and a schedule or
one-off job. A job ran either as a flow or as a plain prompt submitted as a task.

**Measured (`e45_jobs_replay.py`, `~/.north/jobs.db`):**

| Jobs | Flow | Plain prompt |
|---|---|---|
| All time | 550 | 44 |
| Since 2026-09-25 | 550 | **0** |

- The last plain-prompt job ran on 2026-09-24.
- Every way to make a schedule or a one-off job (the chat tool `create_flow`, the Schedules page, the
  built-ins) now makes a flow. A reminder is a flow too.

**Built: the plain-prompt path is deleted.** `_run_scheduled_task`, its task watcher and its exception
are gone. A job always runs its flow. A job with no flow (a row left by an older north) is not guessed
at: `flow_of` ends it as "needs attention" with what to do, and nothing runs. On the 550 real jobs since
2026-09-25, `flow_of` raised for none, so no real job changes.

**Left:** interactive tasks and flow runs are still two runners. Running an interactive request as a
one-step flow is a large change to the orchestrator, with park and resume. It needs its own plan.

## #47: one registry

**Registries on `main`:** agents, tools, skills, flows and MCP servers. The agent registry holds one
agent (`agents/north`): the others last ran in September and October and their folders are gone.

**Measured (`e47_registry_changes.py`, git since 2026-09-01):**

| | Commits |
|---|---|
| All commits | 638 |
| Touched one registry | 23 |
| Touched two or three | 10 (3 since October) |

The 10 include four where one idea went into several registries (needs checks, user overrides, the
evidence workflow, the lifecycle). That is about one such change a month.

**Not built.** One registry for all five would touch 17 files that use the agent registry alone, for
about one double change a month. Proposal, when it is worth it: fold the agent registry first (it holds
one agent), then give tools, skills and flows the one draft, tested, active, retired lifecycle that
skills already have.

## #49: one delivery layer

**Notifiers on `main`:** `TerminalNotifier` (the default), `TelegramNotifier` (cards to Telegram, with
the terminal as fallback), `BatchingNotifier` (wraps the active one), and `MacOSNotifier`.

**Built: `MacOSNotifier` is deleted.** Nothing has built it since 2026-05-22 (1.0.2), `alerter` is not
installed here, and its buttons answered a card with `store.resolve` directly, outside north's approval
API. Notifiers: 4 to 3.

**Not built.** Telegram is still two modules: `approval/telegram.py` sends cards, and
`gateways/telegram.py` takes your messages and follows their tasks. They work in opposite directions,
and a merge has no measured benefit yet.
