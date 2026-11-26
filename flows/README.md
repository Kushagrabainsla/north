# flows

The `flows` package defines flows: named, ordered lists of steps that north can run, test, schedule and resume.
A skill gives know-how. A flow gives the order of the work and keeps a checkpoint of each run.
This package parses, finds and checks flows, and stores run checkpoints. It does not run them.
The runner is `FlowRunner` in `tools/universal/flow_runner.py`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `models.py` | `Flow`, `FlowStep`, `FlowSource`, `FLOW_FILENAME`, `flow_fingerprint` | Frozen value objects for one flow and one step. `flow_fingerprint` hashes the flow and the skills it uses. |
| `registry.py` | `FlowRegistry`, `parse_flow_document` | Reads every `<dir>/FLOW.yaml` from the built-in folder, then from the learned folder. A learned flow replaces a built-in flow with the same name. `require` hides a flow while this machine lacks something it needs. |
| `validation.py` | `validate_flow_capabilities`, `schedulable_flow_error`, `step_tools`, `STEP_TOOLS`, `resolve_execution_tools`, `schema_errors`, `FlowValidationReport` | Checks that each step can run: the skill exists and is active, the inputs match the skill contract, the approval is not below the skill's minimum, and each tool exists. |
| `store.py` | `FlowRunStore`, `FlowRun` | SQLite checkpoints of flow runs (table `flow_runs`): status, current step, inputs and step outputs. It never runs code. |
| `exceptions.py` | `FlowError`, `FlowNotFoundError`, `FlowParseError` | Registry errors. |

## The FLOW.yaml format

```yaml
name: nightly-cleanup          # lowercase letters, digits and hyphens; default is the folder name
description: ...               # required
domains: [general]             # default [general]
status: active                 # candidate | active | retired; default active
steps:                         # a non-empty list; step names are unique
  - name: clean-up
    skill: some-skill          # a skill, or
    instructions: ...          # inline instructions, or
    action: task_context_cleanup   # a server action (built-in flows only)
    inputs: {message: ${inputs.message}}
    approval: on_mutation      # never | on_mutation | before_step
```

- A file in an old format (a step with `tool:` or `agent:`, or a boolean `approval`) still loads. Its status becomes `candidate`, so it must be saved, tested and activated again.
- All built-in flows in `resources/builtin-flows/` use one `action` step each. `orchestrator/app.py` registers each action with `flow_runner.register_action`.

## How north uses it

- `orchestrator/app.py` `_build_flows` makes `FlowRegistry(builtin_dir=resources/builtin-flows, learned_dir=~/.north/flows)`. It hides `nightly-check` with `require` unless a north project folder is registered.
- `orchestrator/app.py` makes `FlowRunStore(~/.north/flow_runs.db)` and calls `pause_interrupted` at startup. The nightly cleanup calls `prune`.
- `tools/universal/flow_runner.py` (`FlowRunner`) runs steps and writes checkpoints. `tools/universal/create_flow.py`, `use_flow.py` and `flow_schedules.py` make, show and schedule flows.
- `orchestrator/api/cron.py`, `tools/universal/flow_schedules.py` and `web/extensions.py` call `schedulable_flow_error` before they put a flow on a schedule.
- `web/extensions.py` parses, checks and lists flows and runs for the dashboard.
- The public interface is `flows/__init__.py` `__all__`: `FLOW_FILENAME`, `Flow`, `FlowRegistry`, `FlowRun`, `FlowRunStore`, `FlowSource`, `FlowStep`.

## Rules

- A step uses a skill, inline instructions or a server action. It never calls a tool directly.
- A step may use only its skill's tools plus `STEP_TOOLS` (`read_tool_output`). Validation rejects a step whose text names a tool the step cannot use (CODING_STYLE §6.5).
- A step's `approval` is a floor set by the skill. A step can make it stricter, never weaker.
- Only built-in flows may use `action`. The parser refuses it in a learned flow.
- A schedule runs with nobody watching. So a flow must be active, unchanged since activation (same `flow_fingerprint`) and valid before it goes on a schedule. Ask `schedulable_flow_error`; do not copy its checks.
- A step that fails pauses the run at that step with its error and earlier outputs. Resume runs that step again, so make steps safe to run again. A missing model or network sets `waiting`, not `failed` (CODING_STYLE §13.5).
- `prune` deletes only finished runs. A running, paused or waiting run is the only copy of its progress.
- The store owns its schema and adds columns with guarded `ALTER TABLE` (CODING_STYLE §11.4).

## Data on disk

- `~/.north/flows/<name>/FLOW.yaml`: learned and user-made flows.
- `~/.north/flow_runs.db`: table `flow_runs`.
