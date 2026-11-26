# north coding scoreboard (evals)

A small, objective harness that measures north's coding ability as a **pass rate**,
so improvements can be driven by data instead of vibes. The pass rate is one number
that you can compare between versions of north.

The suite ships **15 tasks spanning the engineering kinds north routes** (bugfix, feature,
refactor, debug), from one-line fixes to multi-function reasoning. Every task is guarded by
`tests/unit/evals/test_tasks.py`, which asserts its held-out grader fails on the unfixed seed —
so a trivially-passable task can never enter the scoreboard.

## How it works

Each task under `tasks/<id>/` has:

- `task.json` — the `prompt` north is given, plus `kind` and a `timeout_s`. An optional
  `grade` list is the grading command; the default is `python -m pytest -q grade_test.py`.
- `seed/` — the starting workspace (buggy or stubbed code + a minimal test).
- `grade/` — a **held-out grading test** north never sees.

For each task the harness:

1. copies `seed/` into a fresh throwaway git repo,
2. submits the prompt to a running north (with that repo as the workspace),
3. waits for north to finish,
4. copies the held-out `grade/` test in and runs it.

The task **passes iff the held-out test passes** — the same methodology as SWE-bench.
Because the grader is held out, north cannot "teach to the test."

The scoreboard keeps three failure kinds distinct so the number stays honest:

- `fail` — north ran but the grader failed → **north got it wrong**.
- `error` / `timeout` — north/infra could not produce a result (e.g. a model was
  rate-limited, or the task ended as `skipped` because no model was available). These
  are **excluded** from the pass rate rather than counted as coding failures.

## Running it

```bash
# 1. Start north in autonomous approval mode (so mutating tools don't block on cards):
NORTH_APPROVAL_MODE=autonomous .venv/bin/python -m uvicorn orchestrator.app:app --port 8000

# 2. Run the scoreboard (in another shell):
.venv/bin/python -m evals.run_evals
# options:
#   --only fix_average_empty,fix_discount   run a subset
#   --min-pass-rate 0.8                      exit non-zero below this (for CI gating)
#   --base-url http://127.0.0.1:8000         point at a different north
#   --tasks evals/tasks                      read tasks from another folder
#   --work-dir /tmp/north-evals              build the task workspaces here
```

The harness reads north's secret to call the API, and runs the tasks one at a time.
The code is `evals/harness.py` (workspace, grading, report) and `evals/run_evals.py` (the CLI).

> Note: the true ceiling of the pass rate is the underlying **model**. Run against a
> frontier coding model for a representative score; weak/free models will score low
> no matter how good the orchestration is.

## Measuring skill lift (A/B)

To check whether the [skills](../skills/) subsystem raises the pass rate on the *same*
model, run the board twice against servers started identically except for skills:

```bash
# A: skills OFF - point the built-in skills dir at an empty folder
NORTH_APPROVAL_MODE=autonomous NORTH_BUILTIN_SKILLS_DIR=/tmp/empty \
  .venv/bin/python -m uvicorn orchestrator.app:app --port 8000

# B: skills ON - default built-in skills dir
NORTH_APPROVAL_MODE=autonomous \
  .venv/bin/python -m uvicorn orchestrator.app:app --port 8000
```

Run `.venv/bin/python -m evals.run_evals` against each and compare pass rates. North
gives coding work to the installed coding agents, and `orchestrator/coding_briefing.py`
adds the matching `engineering` skills to their brief. The `skill_selected` ledger entries
show which skills north's own agent was offered. Keep graders checking **outcomes** (not skill text) so a skill can't be
"taught to the test". A meaningful A/B needs a capable model and enough runtime for
every task to finish (free/rate-limited models time out and muddy the signal).

## Adding a task

Create `tasks/<id>/` with `task.json`, a `seed/` workspace, and a `grade/` test.
Keep the grader **objective** (assertions on behaviour) and make sure it **fails on
the unfixed seed** — otherwise the task is trivially passable and measures nothing.
