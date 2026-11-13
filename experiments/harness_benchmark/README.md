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

### Score on a cloud machine

A Mac cannot score: the images are x86 only and total over 40 GB. `kit.py` packs everything the scorer needs into one
archive of about 1 MB. It holds north's latest run of each task and each plain run that did work.

1. Build the kit on the Mac:

   ```bash
   .venv/bin/python experiments/harness_benchmark/kit.py build
   ```

2. Create an x86 Linux machine with Docker and about 100 GB of disk, for example a DigitalOcean CPU-optimized droplet
   (8 vCPU, 16 GB) from the Docker image.
3. Copy the kit to it, open it and start the script. Use `tmux`, so the run continues when SSH disconnects:

   ```bash
   scp ~/.cache/north/harness-benchmark/results/scoring-kit.tar.gz root@HOST:
   ```

   ```bash
   ssh root@HOST
   ```

   ```bash
   tar xzf scoring-kit.tar.gz && tmux new -s score ./scoring-kit/score_on_droplet.sh
   ```

   The script first proves the scorer on tasks 2 and 16, then scores every patch. It stops if a proof fails.
4. Copy the scores back, open them, and read the report:

   ```bash
   scp root@HOST:scoring-kit/scores.tar.gz ~/.cache/north/harness-benchmark/results/
   ```

   ```bash
   mkdir -p ~/.cache/north/harness-benchmark/results/final
   ```

   ```bash
   tar xzf ~/.cache/north/harness-benchmark/results/scores.tar.gz -C ~/.cache/north/harness-benchmark/results/final
   ```

   ```bash
   .venv/bin/python experiments/harness_benchmark/kit.py report
   ```

5. Destroy the machine. It bills until you destroy it.

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
