# orchestrator

The `orchestrator` package is north's server and its task lifecycle.
`app.py` holds the FastAPI app and its lifespan, which wires every part and starts the background loops.
The `Orchestrator` class takes a task and runs the goal check when the task acts outside north. Then it runs the task on north (the one agent) and records how it ended.
The package also keeps tasks alive across restarts, streams live events, and connects external coding agents to north's approval layer.

## Main parts

### Task lifecycle

| File | Key names | What it does |
|---|---|---|
| `orchestrator.py` | `Orchestrator` | Submits, runs, pauses, resumes and cancels tasks. Also `send_message`, `respond_approval`, `overrule_approval` and `drain_queued_tasks_loop`. There is no planner model. |
| `models.py` | `TaskRequest`, `TaskResponse`, `Prompt` | The task input and output models. |
| `exceptions.py` | `OrchestratorError`, `NorthStarConflictError`, `TaskCapacityError` | Package errors. `TaskCapacityError` becomes HTTP 429 in `app.py`. |
| `constants.py` | `MAX_CONCURRENT_TASKS`, `MAX_RESUME_ATTEMPTS`, `WATCHDOG_POLL_INTERVAL_SECONDS`, ... | Tuning values. |
| `task_intent.py` | `is_consequential`, `is_repository_overview`, `execution_profile` | Reads a task's intent from its words, with no model call. |
| `north_star.py` | `NorthStarChecker` | Checks a task against the user's goals in `north_stars.md`. |
| `failure_handler.py`, `model_scarcity.py` | `FailureHandler`, `classify_error`, `AgentFailure` | Agent retries, cooldowns, and the kind of a final failure. |
| `result_audit.py`, `verification.py` | `ResultAuditor`, `verify_claims`, `evidence_sufficiency_violations` | Compares what an answer claims with the tools that succeeded. Gives the agent one repair pass, then marks what stays unproven. |
| `journal.py` | `TaskJournal` | Writes a task event to the ledger and to the live stream together. |
| `stream.py` | `EventStreamManager` | In-process publish/subscribe of task events for the SSE routes, with a replay buffer. |
| `idempotency.py` | `IdempotencyCache`, `idempotency_key` | Joins duplicate submissions (for example a webhook sent twice) into one task. |
| `plan_store.py` | `PlanStore`, `PlanStep` | In-memory checklist for each task, used by the `update_plan` tool. |

### Durable state and recovery

| File | Key names | What it does |
|---|---|---|
| `running_tasks.py` | `RunningTaskStore`, `RunningTask` | One row for each task in flight, with its full request and a heartbeat. |
| `reconcile.py` | `recover_interrupted_tasks` | At startup, resumes each task left in `RunningTaskStore`, or fails it when it is too old or resumed too often. |
| `watchdog.py` | `watch_stuck_tasks` | Cancels a task whose heartbeat is too old. A task that waits for your answer is not stuck. |
| `agent_runs.py` | `AgentRunStore`, `AgentRun`, `RunStatus` | One row for each agent run (also delegated runs and retries) and its important events. |

### Server

| File | Key names | What it does |
|---|---|---|
| `app.py` | `app`, `lifespan` | Builds dependencies, tools, skills, flows and the `Orchestrator`. Registers the built-in flow actions, dispatches jobs (`_dispatch_job`), starts the background loops, and mounts the dashboard at `/app`. |
| `background.py` | `BackgroundLoops` | Runs each forever-loop and starts it again after a failure. `/health` shows each loop. |
| `api_context.py` | `ApiServices`, `current_services`, `bind_request_services`, `merge` | The components a request may reach, kept on `app.state` and bound for each request. |
| `api/` | `router`, `health_router`, `webhook_router`, `coding_gate_router`, `coding_ask_router`, `configure` | REST routes, one module for each area: `task`, `approval`, `stream`, `jobs`, `cron`, `ledger`, `runs`, `settings`, `context`, `inference`, `confidence`, `metrics`, `transcription`, `health`, `webhooks`, `coding_gate`, `coding_ask`. `api/deps.py` holds the shared accessors. |
| `idle_update.py` | `IdleUpdate` | Starts a waiting update only when north was idle for the whole window. |
| `nightly_check.py` | `nightly_check`, `registered_checkout` | Runs `scripts/smoke.sh` on a registered north project folder and turns failures into problems with fixes. |

### Coding-agent integration

These files connect the `coding_agents` package to north. `app.py` wires them in `_register_coding_agent`.

| File | Key names | What it does |
|---|---|---|
| `coding_gate.py` | `ApprovalJudge` | Sends each action a coding agent asks for to `Approvals.decide`. |
| `coding_ask.py`, `coding_fetch.py` | `ApprovalsAsker`, `ApprovalsFetcher` | Turns an agent's question, or a page fetch, into an approval card. |
| `coding_briefing.py` | `MemoryBriefing` | Gives the agent the user's facts, profile and matching skills before it starts. |
| `coding_workspaces.py`, `worktree.py` | `GitWorkspaces`, `GitWorktreeManager`, `Worktree` | A git worktree on a throwaway branch for each edit run, with the installed dependencies copied in. |
| `coding_landing.py` | `LandingDesk` | Offers the run's change to the user and applies it to the real working tree when they agree. |
| `coding_run_recorder.py` | `AgentRunRecorder` | Records a coding run as a child row in `AgentRunStore`. |
| `verify_command.py` | `detect_verify_command` | Finds a fixed, known test command for a project, so north runs the tests itself. |

## How north uses it

- `north start` (`cli/_server.py`) and the `Dockerfile` start `uvicorn orchestrator.app:app`.
- The CLI (`cli/main.py`, `cli/tui.py`, `cli/_task_stream.py`), the dashboard (`web/src/`) and `gateways/telegram.py` call the `/orchestrator/...` REST routes.
- `config/dependencies.py` builds `RunningTaskStore`, `AgentRunStore`, `PlanStore` and `EventStreamManager`. `app.py` builds the `Orchestrator` in `_build_orchestrator`.
- `web/api.py`, `web/extensions.py`, `web/views.py`, `web/updates.py` and `web/secrets_panel.py` use `current_services` from `api_context.py`. `web/api.py` also uses `TaskRequest` and `Prompt`.
- `app.py` uses `jobs` (scheduler and processor) and `flows` (registry and run store). See `jobs/README.md` and `flows/README.md`.
- The public interface in `__init__.py` `__all__` is `AgentRun`, `AgentRunStore`, `EventStreamManager`, `FailureHandler`, `NorthStarChecker`, `NorthStarConflictError`, `Orchestrator`, `OrchestratorError`, `TaskRequest`, `TaskResponse`. Callers outside the package import the submodules directly today.

## Rules

- A route calls one function and returns its result. Put the logic in a module (CODING_STYLE §12.2).
- Put a new route on `router` in `api/deps.py`, so it gets `verify_api_access` (CODING_STYLE §12.3). Only `/health`, the webhooks (their own secret header) and the coding gate and ask routes (a token for one run) are outside it.
- A route reads components through `current_services()` or the `api/deps.py` accessors. Do not add module-level state (CODING_STYLE §6.3, §22).
- Write a task event through `TaskJournal`, so the ledger and the live stream always agree (CODING_STYLE §4.11).
- The coding-agent files describe an action. Only the approval layer decides (CODING_STYLE §7.3, §7.5).
- Send a message for a running task to `Orchestrator.send_message`, not `submit_task` (CODING_STYLE §15.4).
- A task stays in `RunningTaskStore` exactly while it is in flight. A resumed task continues from its checkpoint (CODING_STYLE §15.5).
- A missing model or network queues a task. It does not fail it. Cards never time out (CODING_STYLE §13.5).
- Check an agent's claims against tool evidence before the result is recorded (CODING_STYLE §16.1.2).
- Start each forever-loop in the lifespan through `BackgroundLoops.start` (CODING_STYLE §10.4).
- If `MAX_CONCURRENT_TASKS` tasks already work, `submit_task` raises `TaskCapacityError`.

## Data on disk

- `~/.north/running_tasks.db`: tasks in flight (`RunningTaskStore`).
- `~/.north/tasks/tasks.db`: agent runs and their events (`AgentRunStore`).
- `~/.north/smoke/`: nightly check reports and the registered project folder.
- `~/.cache/north/coding-worktrees/`: worktrees of coding edit runs (`GitWorkspaces`).
