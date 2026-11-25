# Context and self-improvement experiments (issues #77, #87, #92)

These experiments use north's own run records. They answer three questions:

1. Does history compaction run?
2. What do meta tools cost a task?
3. Do tool failures recur often enough for a self-refinement loop to learn from them?

The scripts read `~/.north` in read-only mode and print counts only. Their outputs are in `results/`. This bucket changes no product code, so there is no product before/after. The numbers below are the baseline for any later change.

## #92: does compaction run? (`compaction_check.py`)

- **Compaction almost never has a chance to run.** `agents/context_compaction.py` compacts history only when a prompt reaches 75% of the model's window (d55a91d, 2026-10-18). For Opus 5.5 that is 750,000 tokens.
- **Since 2026-11-01:**
  - There were 2,570 model turns. The largest prompt was 167,089 tokens.
  - 102 turns were over 100k tokens.
  - Only 2 turns were over 150k tokens, which is 75% of a 200k window.
- **No compaction call (`:compact`) was ever recorded.** The last tool-summary call was in 2026-10.
- **Decision:** a "surprising result" mark for compaction cannot change anything now, because compaction does not run. Step 3 of #92 (learning from mistakes) already belongs to #77, and the failure data below serves it.

## #87: what meta-programming costs (`meta_tools.py`)

"Meta-programming" here means every authoring tool: `create_tool`, `create_skill`, `create_flow` and `create_view`. Schedules are flows, so they are included. `create_view` was never called.

**Since 2026-11-13** (179 runs; `results/meta_tools_since_2026-11-13.txt`):

| | Count |
|---|---|
| Runs that did meta-programming | 8 (`create_skill` 23 calls, `create_flow` 14) |
| Meta calls that failed (test or check failed, draft refused) | **19 of 37 (51%)** |
| How those runs ended | 8 completed |
| Input tokens per run, median | **299,522** for meta runs, 104,255 for other runs |
| Meta share of all tool output in those runs, median | 79% |
| Tokens that the meta calls (input and result) were re-sent with on later turns | **8.6%** of those runs' input (per run: median 3.9%, max 23%) |
| `create_tool` calls since 2026-10-25 | 0 |

In all runs since 2026-09-01: 27 meta runs, 38 of 99 meta calls failed, the meta runs used a median of 74,567 input tokens against 53,933, and the meta calls made 7.7% of their input.

- **Meta-programming is rare, but where it happens it is costly and unreliable.** About 1 run in 20 builds a skill or a flow. Those runs use about 3 times the tokens of other runs, and half of their meta calls fail and are retried in the main conversation.
- **The 8.6% is a lower bound** for what a child run would remove. It counts only the meta calls themselves, not the reads and edits that the agent made to fix each failure.

**The prompt cache.** A turn right after the tool list grew had 1.1% cached, against 83.1% on other turns (26 turns, 736,758 uncached tokens, 3.2% of all uncached tokens). `find_tools` grew the list most often (10 of 26). Also, `_sync_hot_loaded_tools` adds every tool that appears anywhere in north's registry to every running task, not only the tools that the task built.

**Decision:** #87 should cover all meta-programming, not only tools. The value is real for the runs that do it. The open question is reliability: does a separate run on a strong coding model lower the 51% failure rate? Test that on the failed meta calls before building. Keeping the tool list fixed during a run is a separate, smaller fix (at most about 3% of uncached tokens).

## #77: do failures recur? (`failure_recurrence.py`)

- **Method:** each failed tool result is reduced to a signature: the tool and the first line of its error, with paths, ids, numbers and quoted text masked.
- **Since 2026-11-01:**

| | Count |
|---|---|
| Tool results | 3,321 |
| Failed | 347 (10.4%) |
| Failure signatures | 109 |
| Signatures that happened in 3 or more runs | 24, covering 201 failures (58%) |
| Of those, likely agent mistakes (not environment) | 17 signatures, 130 failures |
| Agent-mistake signatures still seen in the last 7 days | 14 of 17 (85 failures) |
| Failures after a signature's 3rd run (the most a perfect fix at that point could prevent) | 62 (17.9%) |

- **Examples of recurring agent mistakes:**
  - a page script with a top-level `return` (14 runs)
  - clicking an element that another element covers (11 runs)
  - a browser call without a profile id (7 runs)
  - a file name too long for `bash` (5 runs)
  - skill tests that fail in `create_skill` (4 runs)
- **The top one was fixed by hand, but late.** It ran in 14 runs over 9 days, until 464bb9a (2026-11-22). It has not happened since. A digest would have shown it after its 3rd run.
- **Decision:** phase 1 of #77 has measured value. Recurring agent mistakes are concentrated, most still recur, and at most 18% of failures were preventable.
- **Where it belongs:** at task end, not in a nightly report. North learns at task end: `memory/task_end_learning.py` writes the task's note and then learns skills.
  - At each task end, the same step can record the run's failure signatures.
  - When a signature reaches 3 runs, it can be reported (phase 1) and passed to a refiner (phase 2).
- **Why not the extraction pipeline:** the fact extraction pipeline (`memory/extraction.py`) also runs at task end. But it reads only the user's own words, on purpose, so that north never learns the assistant's text as fact. Tool failures come from tool output.

## Limits

- **One user,** and three weeks of runs.
- **Rough causes:** "agent" or "environment" comes from words in the error, not from reading each case.
- **An upper bound:** the 17.9% assumes a perfect fix right after the 3rd run.
