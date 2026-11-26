# Bucket 9: operations, testing and dashboard (#26, #36, #76)

The results hold counts, timings and commit ids only: no prompts and no task text.

```bash
.venv/bin/python experiments/dashboard/e26_task_history.py
.venv/bin/python experiments/dashboard/e76_finding_ids.py
```

## #26: the Tasks page lost all but the last day and a half

The page loaded the latest 500 ledger entries, built its task list from them, and searched them in the
browser. On this north the latest 500 entries began on 2026-11-23 at 17:11: 16 of 421 tasks.

**Fix.** The ledger gives whole tasks: `/orchestrator/ledger?tasks=N` returns every entry of the N tasks
with the newest activity, and `&q=` keeps only tasks that match it (FTS5, each word also as a prefix).
The page asks for 100 tasks, offers "Show 100 more tasks", and sends its search to the server once
typing pauses. "Everything" keeps the raw stream and searches all of it with `/ledger/search`.

The search also split words wrongly. It deleted punctuation, so "Don't" became "Dont" and "fizzbuzz(n)"
became "fizzbuzzn", which no entry holds. It now splits words as the index does. This fixes
`/ledger/search` too.

**`e26_task_history.py`** (on a copy of `~/.north/ledger.db`). The searches are the first two words of
the prompts of 50 tasks outside the latest 500 entries, as a person would type them.

| | Before | After |
|---|---|---|
| Tasks the page reaches (of 421) | 16 | **421** (100 at first, 4 "Show more") |
| Older tasks found by search (of 50) | 0 | **47** on the first page, **50** with "Show more" |
| Time for the first 100 tasks | | 18 ms |
| Time for a search (median) | | 6 ms |

**Not built.** Task controls on the Tasks page (pause, resume, cancel). In the last month the ledger
holds 4 cancels and 6 resumes from every surface together, and none came from a dashboard slash
command.

## #36: keep north running

**Measured** (`~/.north/jobs.db`, 2026-10-25 to 2026-11-25): 443 jobs ran.

| Why a job was late | Jobs |
|---|---|
| On time | 368 |
| The computer was asleep | 64 |
| north was not running | 11 (on 3 days) |

A LaunchAgent helps only with the 11: it does not wake a sleeping Mac, and the owner accepted late
catch-up after sleep. The log cannot tell a crash from a stop: north logs no line when it stops.
Not built; the owner deprioritised it on 2026-09-30.

## #76: the adversarial test's findings

The findings report is not in the repository or on this Mac; the test ran in a cloud session.
**`e76_finding_ids.py`** lists every finding id that a commit citing #76, or the CHANGELOG, names: 73
ids, each with at least one commit (`results/e76_finding_ids.json`). The ids run A1-A20, M1-M24, P1-P16
and W1-W14 with one gap: **W10**, which no commit names. Whether W10 was fixed, and whether the report had
more ids, needs the report.

## Also checked

- **Runs closed weeks late** (seen in bucket 5): 8 runs started from 2026-09-24 to 10-25 were all closed
  on 2026-11-05 as "north stopped while this run was working". The sweep that closes them came in
  7f02913 (2026-11-02) and ran at the next start. No run is open now.
- **Failed runs:** 189 of 426 top-level runs from 2026-10-25 failed, all on or before 11-13, with "no
  model could serve" (bucket 3, fixed in 305232b). None failed since.
