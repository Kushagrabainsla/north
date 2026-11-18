# Browser speed and tokens (issue #83)

Issue #83 asks why browser automation is slow and why it uses so many tokens.
This experiment measures where the time and the tokens go, from north's own run events.

## Files

- `timeline.py` reads the run events of a north home (`tasks/tasks.db`).
  For each run it prints the model time, the time of each browser action, the steps inside each call, and the characters each tool gave the model.
  With no run ids, it reads every run that called the browser.
  Run it with `.venv/bin/python experiments/browser_speed/timeline.py [NORTH_HOME] [--run RUN_ID ...]`.
- `run.py` replays two read-only browser tasks (Hacker News and Wikipedia) through your own TaskWindow bridge.
  It uses a scratch north home and a server started from this checkout.
  It answers cards itself: yes only for a browser card on those two sites, no for all others.
  Results go to `~/.cache/north/browser-speed/<time>/`.
  Run it with `.venv/bin/python experiments/browser_speed/run.py PROFILE_ID [hn] [wiki]`.

The step data comes from the `tool_timing` event (`utils/spans.py`).
Runs from before 2026-11-18 have no steps, only call start and end.

## Results, 2026-11-18

Profile sjsu.edu (existing Chrome), approval mode Autonomous, model `mistral:zai-glm-5-3-flash`.

### Past runs (27 runs that called the browser, 2026-09-23 to 2026-11-18)

- Busy time (wall time less waits for a person): model 115%, browser 57%.
  The model share is over 100% because older events do not mark every wait; the browser share is the reliable figure.
- `browser:eval` was the biggest cost: 404 calls, median 4.7 s each, 4,377 s in all.
- Long runs grew to 100,000 input tokens a turn by turn 120.
  Three runs used more than 8 million input tokens each.

### Replay before the fix (`20261118-105939`)

| | Hacker News | Wikipedia |
|---|---|---|
| Wall time | 57 s | 119 s |
| Model time | 21 s, 8 turns | 44 s, 13 turns |
| Browser time | 32 s | 75 s |
| Memory decider inside the browser time | 27.6 s (86%) | about 41 s (55%) |
| Bridge work (navigate, read, click) | about 4 s | about 8 s, plus 30 s of `wait` |
| Input tokens | 122,929 | 185,995 |
| Fixed prompt (system, tools, memory) per turn | 11,383 (74% of input) | 10,978 (77% of input) |
| Tool results carried on later turns | about 24,700 tokens (20%) | about 35,500 tokens (19%) |

Findings:

1. **Approvals were most of the browser time.** In Autonomous, the memory decider (one memory recall and one model call) ruled on every browser call: 1.3 s to 8.7 s each.
   A yes from the decider did not cover reading the open site, so each later page asked again.
   Only a person's yes kept that scope.
   The live outreach flow of 2026-11-18 showed the same: six decider cards in two minutes, two for the same "Open linkedin.com".
2. **The bridge itself is fast.** The profile check (`bridge.verify`) took 30 to 170 ms, and it runs twice for most calls (once for the card, once for the call).
   The page check for the card (`browser.describe`) took under 155 ms.
   A click (`bridge.computer`) took 2.3 s once.
3. **Each turn resends the fixed prompt**, about 11,000 tokens, of which the browser tool's own schema is about 1,750.
   More turns cost more tokens than larger results in these short tasks.
4. **Browser results hold copies of themselves.** The model gets the result data and a `formatted` copy of it.
   For the default format, the copy is the same JSON again.
   `inspect` holds its tree three times (`snapshot`, `tree` and `formatted`), and `read` holds its text twice.
   Each existing-profile result also carries the profile identity (a 64-character `connection_id` and five more fields).
   A `goto` result is 765 characters for 64 useful; `fill` is 623 for 28.

Fixed after this replay: finding 1 (a decider's yes covers the site, and the decider is told so).

### Replay after the scope fix (`20261118-110404`)

- Hacker News: 45 s (was 57 s); browser time 16.8 s (was 32.1 s).
  `extract` decided in 1 ms with no decider call; the decider still ruled on `goto`, `eval` and `close`.
- Wikipedia: 432 s (was 119 s), for two reasons outside the fix.
  The model took 27 turns and wrote broken arguments (one `profile_id` was about 2,000 characters of noise).
  And `press` ignored its `selector`: Enter went to whatever had focus, the search never ran, and five `wait` calls ran to their 30 s limit (139 s).
  The first replay had the same pattern: two waits, 40 s.

5. **A key press went to the wrong element** (existing profiles). Fixed: `press` now focuses the element it names, as `type` did.

### Replay after the press fix (`20261118-111311`)

- Wikipedia: 135 s, 18 turns. Enter reached the search box; one `wait` ran out (was five).
- 6 of 18 calls from the model were malformed (no `profile_id`, or extra keys of noise).
  One named `84b241fdce825114]` and cost a 28 s decider call before it was refused.

6. **A call for an unknown profile went to the decider first.** Fixed: `unavailable()` refuses it before any card.

### Replay with one copy per result and tab closing (`20261118-114011`)

- Hacker News: 68 s, 9 turns. The model used four `eval` calls this time, and one decider call on them took 17 s.
- Results the model got: `goto` 165 characters (was 765-864), `click` 117 (was 673).
- At task end north saved the comments page as `browser-<profile id>.png` in the task folder and closed the tab.
  The prompt no longer asks the model to close it.

Finding 4 is fixed: the model gets one copy of each result, and no profile-check fields outside `preflight` and `status`.

Not changed yet: finding 3, and the decider call on each `click`, `fill`, `press`, `eval` and `close` (3 to 7 s each).
