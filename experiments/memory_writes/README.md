# Memory writes: issues #88, #90, #46

Bucket 5 of the issue review. Each script reads north's own data in `~/.north` and changes nothing there. The results in `results/` hold counts and hashes only, never fact text.

## #88: a task waits for `remember`

`e88_remember_time.py <label> [checkout]` replays 40 real facts (20 said in chat, 20 found in files, seed 0). Each call runs the checkout's own `RememberTool.run` on a fresh copy of the real fact store, with the fact removed first.

- The `about` label comes from Haiku 5.5, as a task model would give it.
- The contradiction check (`fact_supersede`) is a real Haiku 5.5 call with north's prompt. The first run keeps each answer and its measured time. A later run waits that same time and gets that same answer, so the before and after runs differ only in the code.
- `wait` is how long `RememberTool.run` takes to return. `done` is how long until the background work also ends.
- `compare_e88.py` checks that both runs stored and retired the same facts, call by call.

| | Before (`main`) | After |
|---|---|---|
| Calls that stored the fact | 40 of 40 | 40 of 40 |
| Calls that ran the model check | 39 of 40 | 39 of 40 |
| Task wait, mean | 1.247 s | **0.014 s** |
| Task wait, p90 | 2.279 s | **0.017 s** |
| Task wait, max | 3.520 s | **0.022 s** |
| Calls under 1 s | 23 of 40 | **40 of 40** |
| All work done, mean | 1.250 s | 1.250 s |
| Facts retired by the check | 3 | 3 (the same 3) |

- The model check is 98% of the old wait (1.22 s of 1.25 s). Writing the fact, the embedding and the profile take about 20 ms.
- Haiku 5.5, called directly, is fast. In north's ledger, the 11 timed `fact_supersede` calls took 13.3 s on average (median 1.6 s, max 59 s), on the background models. The old wait was then that long.

## #90: a fact changes while a task that loaded it runs

`e90_changes_during_runs.py` counts the runs that had a fact change (a new fact, or one retired) inside their time window. Every prompt carries the profile, so every run loads every current fact. This is an upper bound.

- 10 of 2,140 top-level runs (0.47%) had a change inside their window.
- 8 of the 10 are not real: those runs were closed 11 to 41 days after they started, so their window covers weeks.
- In the other 2, the task did not need the changed facts (a news briefing while a file scan added facts), or the facts came from the user's own message in that same task.
- No run in the records acted on an old fact. Nothing was built.

## #46: recall facts by the named things a task mentions

`e46_entity_recall.py` uses bucket 1's 120 labelled runs (#85): a judge said which facts in the prompt the answer used. Haiku 5.5 lists the named things in each fact and each prompt. Entity recall keeps a fact when it shares a named thing with the prompt.

| | Now | Entity recall |
|---|---|---|
| Facts in prompts | 910 | 93 (90% cut) |
| Used facts kept (of 30) | 30 | **7** |
| Runs where every used fact stays (of 21) | 21 | 5 |

- 67 of 120 prompts name no entity at all.
- Most used facts are about the user's name and identity, and their prompts are "hey" or "what do you know about me?".
- The data does not support an entity store for recall. Nothing was built.
