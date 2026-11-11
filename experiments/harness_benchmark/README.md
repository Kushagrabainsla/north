# North vs Claude vs Codex on published coding benchmarks

## Question

If you give a coding task to north, and north hands it to Claude and Codex, is the result better than giving the same
task to plain Claude Code or plain Codex?

## Setups

| setup | what runs |
|---|---|
| `north` | A real north server with its real model and an empty home. It reads the prompt and decides what to do. Yolo mode, so nobody has to approve anything. |
| `claude` | `claude -p`, headless. Project settings only, so your global rules do not apply. |
| `codex` | `codex exec -s workspace-write`, headless. |

No prompt names a coding agent, and each setup gets the same words.

## Tasks

Twenty tasks in `catalog.py`, from SWE-bench Pro V2, Terminal-Bench 4.1, FeatureBench, SWE-EVO, c-CRAB and CodeIPI.
The prompts are each benchmark's own text. The only edit is that a container path (`/app`, `/workspace`) is replaced
by the copy's real path, the same for every setup.

## Method

1. `fetch.py` downloads the tasks once, to `~/.cache/north/harness-benchmark`.
2. `run.py` makes a fresh copy of each task on this Mac, with no git history and the same installed dependencies,
   runs one setup, saves the patch, and deletes the copy. One run at a time. A finished run is skipped on restart.
3. `score.py` applies each patch to a clean copy of the benchmark's official image and runs its own verifier, through
   [Harbor](https://github.com/harbor-framework/harbor). The two injection tasks are scored during the run.
4. `report.py` prints the tables, including how often north won or lost against each setup on the same task.

## Run

```bash
uv run --no-project --with pyarrow python experiments/harness_benchmark/fetch.py
```

```bash
.venv/bin/python experiments/harness_benchmark/run.py --samples 3 --name full
```

```bash
uvx --from harbor python experiments/harness_benchmark/score.py --name full
```

```bash
.venv/bin/python experiments/harness_benchmark/report.py ~/.cache/north/harness-benchmark/results/full
```

To prove the scorer on a task first (the reference fix must pass and an empty change must fail):

```bash
uvx --from harbor python experiments/harness_benchmark/score.py --check 2
```

## Checked so far

- Scorer, on task 2: SWE-bench Pro's reference fix passes and an empty change fails. The other verifiers are unproven.
- All three setups ran end to end on tasks 19 and 20. One sample each, so this says nothing about which is better.

## Limits

- North's prompt limit is 32,768 characters. Task 15 is 42,552, so north will refuse it unless the limit changes.

- The agents work on macOS, while the verifiers run on Linux. An agent can only run a project's tests here if its
  dependencies install on a Mac. `setup_failed` in each record says when they did not. Every setup has the same limit.
- Scoring needs about 45 GB of images, and the FeatureBench images are over 10 GB each. Run `score.py` on a machine
  with room. The results folder holds plain patch files, so it can be copied.
- `kv-live-surgery` from Terminal-Bench is replaced by `wal-recovery-ordering`: it needs a live server under load in a
  container, and north does not run inside one.
- Each run is capped at 50 minutes, SWE-bench Pro's limit. Terminal-Bench allows 8 hours.
- Task 18 (code review) saves each setup's findings but is not scored yet. c-CRAB scores a review by having a coding
  agent apply it and running held-out tests, which needs its own pipeline.
- Runs spend your Claude and ChatGPT plan quota.
