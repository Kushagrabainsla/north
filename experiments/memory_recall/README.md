# Memory recall experiments (issues #85, #86, #89, #91)

These experiments replay north's own past tasks against a snapshot of the memory.
They answer four questions:

1. Which of the memory a task gets does it use?
2. Must every task carry the whole profile?
3. Must a task search memory again when its focus changes?
4. Does a use score rank memory better than similarity alone?

The personal data stays in `~/.north/experiments/memory_recall`: the prompts, facts, answers and labels.
The folder `results/` holds only counts and rates.

## Data

- **Tasks:** 196 replayed tasks. Each one is a distinct prompt of a finished top-level run of the general agent, from 2026-09-05 to 2026-11-24: 171 chats and 25 flow steps.
- **Snapshot:** the snapshot of 2026-11-24 holds 288 active facts, 201 notes of past tasks, and a profile of 12,462 characters (14 sections).
- **Recall:** the replay recall equals production recall in 20 of 20 checked tasks (`build_dataset.py`).
- **A leak, found and removed.** The snapshot holds memory written after a task ran. 311 of the 588 notes and 292 of the 1,438 facts that the replay loaded did not exist when their task ran. Some notes were written about that same task.
  - `cases_asof.jsonl` gives each task only the memory that existed at its start (`leak_check.py`). The experiments for #89 and #91 use it.
  - The profile has no history. So the profile counts may still contain this leak.

## Labels

- **Reference:** Opus 5.5 at medium effort marks each loaded item "used" or "not used".
  - It sees the task, the tool calls and the final answer.
  - A second Opus pass agreed with the first at kappa 0.92 (30 tasks).
- **Cheap judges:** each was scored against the reference on a held-out half of the tasks (60 tasks, about 1,470 items).

| Judge | Kappa | Precision | Recall | Median time |
|---|---|---|---|---|
| words in the answer (no model) | 0.41 | 0.44 | 0.50 | - |
| embedding near the answer (no model) | 0.38 | 0.43 | 0.46 | - |
| Haiku 5.5, low effort | 0.58 | 0.60 | 0.65 | 3.4 s |
| Sonnet 5.5, low effort | 0.76 | 0.68 | 0.94 | 5.9 s |
| Haiku 5.5, medium effort | 0.79 | 0.74 | 0.90 | 8.8 s |
| Sonnet 5.5, low effort, quote rule | 0.80 | 0.80 | 0.85 | 6.5 s |
| **Haiku 5.5, medium effort, quote rule** | **0.83** | **0.82** | **0.88** | 9.9 s |

- **Quote rule:** the judge copies the words of the answer or an action that show each use. A quote that is not found there does not count.
- **Product judge:** `memory/memory_use.py` uses this judge (`e85_attribution.py`, `e85b_variants.py`).

## Results

### #85: which memory tasks use (before numbers for this bucket)

These counts come from the reference labels, 120 tasks, with only the items that existed at task time:

| Kind | Loaded | Used | Used share |
|---|---|---|---|
| Facts | 729 | 21 | 2.9% |
| Notes of past tasks | 173 | 31 | 17.9% |
| Profile sections | 1,680 | 149 | 8.9% |

Use of each profile section (120 tasks):

| Section | Tasks that used it |
|---|---|
| Judgement rules | 58 |
| North stars | 31 |
| Identity | 12 |
| Jobs | 11 |
| Education, Projects | 8 each |
| Preferences | 6 |
| Schedule | 5 |
| User | 4 |
| Skills | 3 |
| Health, Finances, Other | 1 each |

### #86: the profile (`e86_profile.py`, `e86b_variants.py`, `after_86.py`)

- **Similarity cannot choose the sections.** With similarity, the extra sections included only 2 of the 38 used non-core sections, for about 300 more tokens. So the variants that chose sections by similarity were dropped.
- **Live test.** Opus 5.5 answered 34 questions:
  - 12 real past prompts that used a non-core section
  - 5 broad questions
  - 7 questions about one section each
  - 10 tasks that were not about the user

  Sonnet 5.5 graded each answer from 0 to 2 against the full profile.
- **Noise.** When the same setup was answered and graded again, 28% of the grades changed (1.32 against 1.27). So, with 34 questions, a difference smaller than about 0.2 is noise.

| Profile given | Start tokens | Mean grade |
|---|---|---|
| Full profile, cut at 12,000 characters (today) | 3,205 | 1.32 / 1.27 (two runs) |
| Whole profile, no cut | 3,321 | 1.24 |
| Whole profile, no repeated facts | 3,256 | 1.21 |
| Core only: identity, preferences, user, goals, rules | about 1,200 | 1.24 (broad questions 1.0) |
| Core + `read_profile` tool | about 1,200 | 1.06 (tool called on 9% of questions) |
| Lean profile: no skills, health, finances, other | 2,444 | 1.09 |

- **Decision.** Keep the whole profile. Do not put it behind a tool. The model seldom calls the tool, and published agent studies report the same.
- **Change 1.** Order the profile so that the cap cuts only the least-used topics.
- **Change 2.** Skip a recalled fact that the profile already shows.
- **Measured with the product code:**
  - Before: the cap cut 462 characters of the judgement rules.
  - After: it cuts 380 characters of Finances and 85 of Other.
  - Recall skips 346 of 1,438 recalled facts (24%), because the profile shows them.

### #89: search memory again on a change of focus (`e89_research_again.py`)

- **Setup:** 98 tasks with two or more tool calls. The cues were a new path, site, name, plan step, or any step. The filters were "above the best start score", "0.6 or more" and "passes the start cutoff". Each task also got the cues of an unrelated task as a control.
- **No cue tells a real change of focus from noise.** The control cues fired as often as the real ones. For example, path cues fired in 65% of tasks, and in 68% with the control cues.
- **96% to 99% of the added items were not needed.** The best trigger (every step, above the best start score) added a needed item in 16 of 98 tasks. It also added 453 tokens a task, and the control cues added a needed item in 7 tasks.
- **Decision:** do not build it now.

### #91: a use score in ranking (`e91_fading.py`, `e91b_sweep.py`)

- **Setup:** 196 tasks in time order, with the memory that existed at each task. The 30 nearest facts and the 6 nearest notes were candidates, and Haiku 5.5 (medium effort) marked each one "needed".
- **Learning and scoring:** policies learned from the first half of the tasks and were scored on the second half (98 tasks; 32 of them needed any item).
- **Held-out check:** the weights were chosen on the first 16 of those 32 tasks and scored on the other 16.

| Policy | Recall of needed items | Items / tokens a task |
|---|---|---|
| Similarity (today) | 0.62 | 11.1 / 630 |
| + use rate, weight 0.1 | 0.68 (gain 0.06, 95% interval 0.01 to 0.11) | 11.0 / 660 |
| + use rate and 60-day fade (#91 as written) | 0.66 | 11.1 / 638 |
| + recency of the fact | 0.63 | 10.9 / 618 |
| 5 facts and 2 notes, no use data | 0.52 | 6.5 / 384 |

| Setting chosen on held-out data | Gain on the other half | 95% interval |
|---|---|---|
| use rate only | +0.12 | 0.04 to 0.20 |
| use rate and fade (#91) | +0.09 | -0.03 to 0.21 |
| recency only | +0.10 | 0.00 to 0.20 |

- **Needed facts outside today's top 15:** 34% of the needed facts ranked 16 to 30, where today's top 15 never shows them.
- **An exploration place** (#91 step 4) found no needed item.
- **Decision:** add only the use rate. The rate is (used + 1) / (loaded + 2), and the weight is 0.1. Recall asks twice as many candidates, so a used fact just past the top can move in.

## Before and after, with the product code (`verify_before_after.py`, `dump_pools.py`)

The same script ran north's own code on `main` (before) and on this branch (after), on one copy of the memory and the same 196 prompts. The results are in `results/verify_before.json` and `results/verify_after.json`.

| | Before (main) | After (this branch) |
|---|---|---|
| Profile text that the 12,000-character cap cuts | 467 characters of the judgement rules | 382 characters of Finances, 85 of Other |
| Facts recalled for 196 prompts | 1,438 | 1,092 |
| Recalled facts that the profile already shows | 348 | 0 |
| Mean memory tokens a task | 3,605 | 3,560 |
| Recalls that give item ids | 0 | 192 (the other 4 found no facts) |
| `memory_used` events after 20 replayed tasks | none: no judge | 20 of 20 |
| Agreement of the product judge with the Opus reference (524 items) | - | 96.4% (precision 0.75, recall 0.81) |
| Tasks where the product ranking keeps the facts that the #91 replay measured | - | 98 of 98 |

- **Judge setup:** the product judge (`memory.memory_use.judge`) ran with north's own provider and Haiku 5.5.
- **Ranking check:** `dump_pools.py` saved the replay's candidates and picks. The product `_apply_recall_cutoff` got the same candidates and use scores.

**Tests.** The new tests of this branch were also run against the code on `main`:

| Tests (3 files, 65 tests) | On main | On this branch |
|---|---|---|
| `tests/unit/memory/test_memory_use.py` (9) | cannot import: `memory.memory_use` does not exist | 9 pass |
| profile order test | fails: the judgement rules come after Finances | passes |
| agent hook test | fails: `agents.base` has no judge | passes |
| all other tests in those files | 54 pass | 54 pass |

## Limits

- **One user.** All data is from one user's memory.
- **Counts:** 120 labelled tasks for #85, and 32 tasks that needed any item for #91.
- **The judge is a model**, not ablation: no hosted model gives the log-probabilities that ablation needs.
- **The profile has no history**, so the profile may still contain the leak.
- **The #86 answers are single model turns**, without north's tools.

## Cost

The model calls for all experiments cost about $20 (the runs that the cache did not answer).
