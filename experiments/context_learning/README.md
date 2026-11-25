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

## #87: what meta tools cost (`meta_tools.py`)

**Since 2026-11-13** (175 runs; in all runs since 2026-09-01 the picture is the same):

| | Count |
|---|---|
| Runs that called `create_tool`, `create_skill` or `create_flow` | 8 |
| Their meta-tool share of all tool output, median | 79% (all 8 runs at 3.5% or more) |
| `create_tool` calls since 2026-10-25 | 0 |
| Turns right after the tool list grew | 26 |
| Cached share of those turns | 1.1% (other turns: 83.1%) |
| Uncached tokens on those turns | 736,758, which is **3.2%** of all uncached tokens (22.9 million) |
| Tool called just before the list grew | `find_tools` 10, `bash` 5, `browser` 4, others 7 |

- **The cache loss is real, but small.** A tool list that grows during a run breaks the cached prompt prefix: tools come first in the prompt. But it happened on 26 of 2,089 later turns.
- **`find_tools` causes it more than the meta tools.** Also, `_sync_hot_loaded_tools` adds every tool that appears anywhere in north's registry to every running task, not only the tools that the task built.
- **Decision:** the meta tools are too rare now to justify building them in their own child run. A general fix that keeps the tool list fixed during a run could save at most about 3% of uncached tokens. That is a follow-up, not this issue.

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
- **Decision:** phase 1 of #77 (a read-only failure digest) has measured value. Recurring agent mistakes are concentrated, most still recur, and at most 18% of failures were preventable. How to deliver it (a section of the nightly check, or its own flow) is the owner's decision, because north is in a feature freeze.

## Limits

- **One user,** and three weeks of runs.
- **Rough causes:** "agent" or "environment" comes from words in the error, not from reading each case.
- **An upper bound:** the 17.9% assumes a perfect fix right after the 3rd run.
