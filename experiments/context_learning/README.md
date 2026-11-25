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

### Why meta calls fail, and does a child run help? (`e87_skill_check.py`, `e87_child_run.py`, `e87_prompt_changes.py`)

**Why the calls failed.** 13 of the 19 failed meta calls since 2026-11-13 were `create_skill` drafts that failed the selection check, in 4 runs. The other 6 were flow drafts with missing inputs, a blocked path, a paused test run, a wrong schedule field and an unknown tool name. Each of those passed within 1 or 2 retries.

**The selection check does not test what live tasks do.**

| | The `create_skill` check | Live tasks (`agents/base.py`) |
|---|---|---|
| Similarity floor | 0.35 (the selector's default) | 0.70 (`_SUGGESTED_SKILL_MIN_SIMILARITY`) |
| Skills it competes against | every skill in north | only the skills of the agent's domain |

`e87_skill_check.py` rebuilt all 23 real drafts that carried example requests, and ran north's real `SkillSelector` under both rules (`results/e87_skill_check.json`):
- Of the 13 drafts that failed the check, only **1** would pass the live rule. So the check is not too strict.
- Of the 6 drafts that passed the check, a live task picks the skill for only **11 of their 21** example requests. So the check is too loose.
- The replay matched the recorded check outcome in 16 of 19 cases. The skill set has changed since then.

**How skills really reach tasks** (since 2026-11-13):
- Flow steps: all 64 name their skill directly.
- Chat runs (119): similarity suggested a skill in 10, and the agent called `use_skill` in 7.

**How the main runs passed.** In all 4 runs, the agent changed its own example requests until the check passed (`results/e87_prompt_changes.txt`). For the two hard skills, it replaced 4 of 5 positive requests, and then all 5 positive and 4 of 5 negative requests.

**The child-run test.**
- **Setup:** each of the 4 skills went to a fresh child run (Opus 5.5, high effort). The child got the purpose, the procedure and the user's example requests, and one tool that runs north's real selection check and writes nothing.
- **Rules:** the example requests stayed fixed. The child could change only the name and the description, at most 8 attempts.
- **Results:** `results/e87_child_run.json`.

| | Skills that passed | Failed attempts per skill | Model tokens (all 4) |
|---|---|---|---|
| Main run (as recorded; it could change its examples) | 4 of 4 | 1, 3, 1, 8 | inside runs of ~300k tokens each |
| Child run, today's feedback (only the failed requests) | 1 of 4 | 4, 8, 8, 8 | 85,915 |
| Child run, plus each score, the floor and the rival skill | 2 of 4 | 2, 8, 3, 8 | 82,130 |
| Child run, full feedback, live rule | 2 of 4 | 0, 8, 0, 8 | 67,931 |

- **A child run does not lower the failure rate.** With fixed examples, it passed fewer skills than the main runs, which passed by rewriting their tests.
- **The child run saves little context.** It used about 17,000 to 21,000 tokens per skill. The meta work inside a main run was about 26,000 tokens (8.6% of a median 300k run).
- **Scores in the feedback help:** 2 passes instead of 1, with fewer attempts. Under the live rule, two skills passed on the first try.
- **Two skills never passed.** This embedding model cannot separate their example requests from other skills by name and description alone.

**Decision:** do not build the child run for #87. The cause of the meta-programming failures is the skill selection check:
1. It does not match the live rule.
2. The agent can rewrite its own test cases.
3. Its feedback has no scores.

Fixing the check is the general fix. How strict it should be is the owner's decision (see the #87 comment). Keeping the tool list fixed during a run is a separate, smaller fix (at most about 3% of uncached tokens).

### Does the model pick the right skill from a list better than the score? (`e87_list_vs_score.py`)

**Do we need the test at all?** The open Agent Skills standard shows the model every skill's name and description, and the model chooses. It has no selection test.
- North gives a task a skill's description only when the skill scores 0.70 or more. Every other skill is listed by name only.
- This test compares the two ways on the real requests, with the 24 skills of the `general` domain.

**Setup**
- **Chat requests:** the 168 distinct prompts of north's finished chat runs. The ground truth comes from a judge (Opus 5.5, high effort) that saw every skill in full: name, description and steps. It said that a skill fits 50 of the requests.
- **Example requests:** the 64 example requests of the skill drafts whose skills exist now. The draft gives each label, so these labels do not depend on the judge.
- **Pickers:**
  - `score`: north's real `SkillSelector`, as a live task calls it (floor 0.70, the domain's skills; up to 2 picks).
  - `list_opus`: Opus 5.5, medium effort, reads every name and description and answers one skill or none.
  - `list_haiku`: the same with Haiku 5.5, low effort.
- **Results:** `results/e87_list_vs_score.json`.

**Chat requests:**

| Picker | Right skill found (of 50 that need one) | Picked a skill when none fits (of 118) | Overall right |
|---|---|---|---|
| score (today) | 7 (14%) | 3 | 72.6% |
| list, Opus 5.5 | **49 (98%)** | 27 | **83.3%** |
| list, Haiku 5.5 | 26 (52%) | 17 | 75.6% |

**Example requests from the drafts:**

| Picker | Must-pick requests picked | Must-not requests kept out | Overall right |
|---|---|---|---|
| score (today) | 8 of 34 (24%) | 29 of 30 | 57.8% |
| list, Opus 5.5 | 32 of 34 | 30 of 30 | 96.9% |
| list, Haiku 5.5 | 34 of 34 | 29 of 30 | 98.4% |

- **The score misses most skills.** A task gets the right skill for only about 1 request in 7 that needs one.
- **A list of names and descriptions, read by the task model, finds almost all of them.** Its cost is extra picks when no skill fits (27 of 118). In a real task, an extra pick costs one `use_skill` read. In this test, the model had to choose before it worked.
- **Limit:** the judge is also Opus, so `list_opus` may agree with it more easily. The example requests have their own labels, and the list wins there too.

**Decision:**
- **How tasks pick a skill:** show every task the name and description of every skill of its domain, and let the task model choose. This replaces the similarity suggestion.
- **`create_skill`:** drop the selection test and the example requests. Check the recipe (name, a description with what the skill does and "Use when ..."), the format, the size, the tool names, and that the skill is not a near-copy of an existing skill.

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
