# Model contention experiments (issues #8, #93, #94)

These experiments use north's own routing records and the user's real prompts. They answer three questions:

1. Does background work still compete with the task the user asked for?
2. Does a model's reasoning strength (effort) change the answers enough to need a setting?
3. Can north use a local model on this Mac now?

The scripts read `~/.north` in read-only mode. The prompts and answers stay in `~/.north/experiments/model_contention`. The folder `results/` holds counts only.

## #8: background against the foreground (`contention.py`, `quota_replay.py`)

**What the records show.** Each row of `routing_decisions` in `models.db` is one routed model call.

| Period | Calls | Background | Task calls that failed | Failed task calls with a background call in the 60 s before |
|---|---|---|---|---|
| 2026-09-01 to 11-24 | 8,577 | 1,642 (19%) | 1,887 of 6,098 | 93 (4.9%); for successful task calls: 29.1% |
| 2026-11-13 to 11-24 | 3,422 | 355 (10%) | 25 of 2,289 (1.1%) | 2 (8%); for successful task calls: 5.4% |

- **Background work did not cause the failures.** Task calls failed because of rate limits, a spent free allowance and outages. They did not fail because background work used the quota first. Before 11-13, failed task calls followed background calls *less* often than successful ones did.

**What a free tier would do.** `quota_replay.py` replays the same timeline against a simulated limit of N calls a minute for all of north. "Shared" means that background and tasks take places in turn. "Yield" means that a background call waits while a task call happened in the last 60 s.

| Limit | Task calls refused, shared | Task calls refused, yield | Background delayed (median) |
|---|---|---|---|
| 10 a minute | 25.1% | 26.3% | 412 s |
| 20 a minute | 4.6% | 5.1% | 202 s |
| 30 a minute | 1.2% | 1.1% | 200 s |

(Timeline from 2026-11-13. From 2026-09-01 the result is the same: yield is not better, for example 2.2% against 3.5% at 20 a minute.)

- **Priority for tasks would not help.** Tasks make 90% of the calls, in bursts of their own. When background work waits, it only piles up and runs at the next quiet moment, which is also when the next task starts.
- **The two contenders that #8 names do not compete now:**
  - Bootstrap starts only when the user asks (db318a3).
  - `EpisodeConsolidator.run` and `SkillDistiller.run` were endless loops that nothing started any more. The consolidator and the distiller run at the end of a task and in hourly built-in flows. The dead loops are deleted.
- **One finding from bucket 2 is also fixed.** "tool_calls suspended on this model" failed 120 runs. All of them were on 2026-11-10 and 11-11. 305232b (11-13) asks once more after an empty reply, and it no longer suspends a model that served tool calls before. There were 0 such failures after that.

## #8 before and after, in the code (`verify_before_after.py`)

The same script read the source of `main` (before) and of this branch (after). It parsed every file of north's own code, leaving out tests and experiments. The results are in `results/verify_before.json` and `results/verify_after.json`.

| | Before (main) | After (this branch) |
|---|---|---|
| `EpisodeConsolidator.run`: an endless loop | yes | removed |
| `SkillDistiller.run`: an endless loop | yes | removed |
| Calls of either `.run()` in north's code | 0 | 0 |
| Calls of `.run_once()` (the hourly flows, `orchestrator/app.py`) | 2 | 2 (the same) |
| Tests of the consolidator, the task-end learning, the skills and the scheduler (316) | 316 pass | 316 pass |

- **The loops were dead.** They existed before, and no code called them. So deleting them does not change what north does.
- **The real paths are the same.** The task-end path (`memory/task_end_learning.py`, through `catch_up` and `run_once`) and the hourly flows are the same in both versions, and their tests pass in both.
- **Full suite on this branch:** 4,290 passed, 52 skipped.

## #94: reasoning strength (`e94_effort.py`)

**Setup**
- **Prompts:** 30 past chat prompts of the north agent, each answered with at most one tool call.
- **Answers:** Opus 5.5 (the user's model for tasks) at effort low, medium (its default) and high, in one turn with no tools.
- **Judge:** Sonnet 5.5 compared each low and each high answer with the medium answer, in random order.

| Effort | Median time | Mean output tokens | Against medium (better / equal / worse) |
|---|---|---|---|
| low | 3.8 s | 498 | 2 / 14 / 14 |
| medium | 4.0 s | 583 | - |
| high | 4.8 s | 668 | 7 / 14 / 9 |

- **Medium, the model's own default, is the best of the three for these tasks.**
  - Low saves 5% of the time and 15% of the output, but it is worse in 14 of 30 answers.
  - High is 20% slower and is not better overall.
- **Decision:** an effort setting in the Models panel would give little value now. North already leaves each model at its default level.
- **Limits:** these were single turns with no tools. Long agent tasks and coding can gain from more effort, and coding agents often use a higher level by default. Each answer was judged once.

## #93: a local model (no script)

- **No local model server is installed on this Mac:** no `ollama`, `llama-server` or LM Studio.
- **The Mac has 16 GB of memory.** That is enough only for a small quantized model with a short context, as the issue says.
- **Decision:** the issue is a new feature during the freeze, and it says to start it only when the owner decides. Installing a server needs the owner's yes.

## Cost

The model calls for #94 cost about $3.
