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
  Run it with `.venv/bin/python experiments/browser_speed/run.py PROFILE_ID [hn] [wiki] [chain] [three]`; name a task again to run it again.

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

### A/B: before (`766e6d6`) and after (`f4483c4`), 3 runs of each task

Same harness and prompts, run one after the other on 2026-11-18 (`20261118-115552` before, `20261118-120551` after).
Compare them with `compare.py BEFORE_DIR AFTER_DIR`. Medians:

| | Hacker News before | after | change | Wikipedia before | after | change |
|---|---|---|---|---|---|---|
| Wall time | 72.6 s | 42.5 s | -42% | 117.5 s | 92.1 s | -22% |
| Browser time | 48.2 s | 11.3 s | -77% | 80.9 s | 64.6 s | -20% |
| Browser time per call | 5.7 s | 1.9 s | -67% | 7.4 s | 4.3 s | -41% |
| Turns | 9 | 8 | -11% | 11 | 16 | +45% |
| Input tokens | 155,314 | 132,422 | -15% | 142,667 | 205,146 | +44% |
| Input tokens per turn | 17,257 | 14,577 | -16% | 14,252 | 13,377 | -6% |
| Cost | $0.024 | $0.021 | -13% | $0.022 | $0.031 | +41% |

- Time per browser call fell in both tasks: most calls no longer wait for the decider.
- Tokens per turn fell by 6-16%. Most of each turn is the fixed prompt (about 11,000 tokens), which did not change.
- Total tokens follow the number of turns, which the model chooses. The Wikipedia after runs took more turns: the model left out `profile_id` and retried, and searched in more steps. Both versions show such retries.
- 3 runs per task is a small sample; a single run varied from 66 s to 125 s.

### Batch read: three pages in one call (`20261118-124428` before, `20261118-124700` after)

Task `three`: read three Wikipedia articles and give each person's birth year. 3 runs each, `f4483c4` against the batch code.

| Median | before | after | change |
|---|---|---|---|
| Wall time | 44.5 s | 28.5 s | -36% |
| Turns | 7 | 4 | -43% |
| Browser calls | 4 | 2 | -50% |
| Input tokens | 113,756 | 58,194 | -49% |
| Cost | $0.019 | $0.010 | -48% |

- All 3 after runs used one `read` with three `urls`.
- The three pages loaded in parallel in about 1 s (9.4 s in the first run, which also created North's window).
- Most of the batch call is now the decider's one ruling on the new site: 1.2 s to 15 s.
- Before, the second and third reads already took 0.27 s each (the scope fix); the saving is the model turns between them.

Not changed yet: finding 3, and the decider call on each `click`, `fill`, `press`, `eval` and `close` (3 to 7 s each).

## Results, 2026-11-22: step lists

Profile sjsu.edu (existing Chrome), approval mode Autonomous, model Opus 5.5 (the user's routing that day).
Before is `15bd2fc`; after is the step-list code (`action='steps'`, one card per list, the page view after a click, Enter sent with `"\r"`, search not counted as a send).
New task `chain`: search Wikipedia for Ada Lovelace, then follow links to Charles Babbage and the Analytical Engine.

### Past runs (30 runs that called the browser, 2026-11-10 to 11-22)

- A run made a median of 31 browser calls (113 at most); 631 of 1,158 calls were `eval`.
- Model time between browser calls: 130 min in all, median 2.5 s. Browser actions: 11 min.
- The memory decider took 3.5 to 4.3 s on each click and fill, 13 min in all.
- 10% of calls failed (117), mostly a missing profile, a covered or removed element, a script with a top-level `return`, and waits that ran out.

### A/B, 3 runs of each task (`20261122-202157` and `20261122-202754` before, `20261122-204226` after)

| Median | wiki before | after | change | chain before | after | change |
|---|---|---|---|---|---|---|
| Wall time | 17.2 s | 10.1 s | -41% | 44.6 s | 37.5 s | -16% |
| Browser time | 7.5 s | 3.7 s | -50% | 24.3 s | 20.7 s | -15% |
| Turns | 6 | 3 | -50% | 17 | 8 | -53% |
| Browser calls | 5 | 2 | -60% | 18 | 7 | -61% |
| Input tokens | 100,088 | 52,810 | -47% | 308,417 | 170,966 | -45% |
| Cost | $0.026 | $0.021 | -22% | $0.082 | $0.065 | -20% |

- Every after run answered correctly. Each wiki run was one list: goto, fill, press Enter, wait, query.
- Input tokens per turn rose 3-13%: a list result carries the page view. Fewer turns more than make up for it.

Two rounds in between were slower, and showed two bugs that the single calls had hidden:

1. **Enter never submitted a form** in existing profiles. TaskWindow's `computer` key action sends a named key as `rawKeyDown` with no `text`, and Chrome submits a form only on an Enter that carries `"\r"`.
   In the before runs the model pressed Enter, saw nothing happen, and clicked the Search button instead.
   In a list, the wait after Enter ran to its 15 s limit (4 of 6 lists). Fixed: north sends Enter through TaskWindow's typing, which sends a real Enter with `"\r"`.
   A pause until the page was quiet was tried first and did not help, so it was removed.
2. **Enter in a search box counted as a send**, so the list stopped before every search. Fixed: a search field, or a control in a search form, sends nothing unless its own words say send.

Not changed yet: `eval` with a top-level `return` still fails (`SyntaxError: Illegal return statement`), and a click on an element the last page change removed still fails with "inspect again".

## Results, 2026-11-22: extension update, click wait, kept profile (`0c2511f`)

### The extension update reaches Chrome

The first check of the sjsu.edu profile logged "Updated sjsu.edu's browser extension files; reloading the extension", and the task went on with the pairing intact.
The profile's copy now has `evaluate.js`.
On the Hacker News task, `eval` scripts with a top-level `return` no longer fail:

| | before (`56c71bd`, `20261122-202413`) | after (`0c2511f`, `20261122-222553`) |
|---|---|---|
| Runs with "Illegal return statement" | 2 of 3 | 0 of 3 |
| `eval` calls in those runs | 3 | 2 |

### Read-only LinkedIn and Gmail tasks, 3 runs each (`15bd2fc` against `0c2511f`)

A scratch copy of `run.py` (not in the repository) with three tasks in the sjsu.edu profile: search LinkedIn for people at Anthropic with "research" in their title and read three profiles; list the first five LinkedIn notifications; list the five newest Gmail inbox emails.
The scratch north asked on every change, and the harness rejected any card that would send or spend.
Results are in `~/.cache/north/browser-heavy/20261122-222003` (before) and `20261122-222254` (after).

| Median | li_search before | after | li_notes before | after | gmail before | after |
|---|---|---|---|---|---|---|
| Wall time | 21.5 s | 17.5 s | 12.1 s | 10.3 s | 13.2 s | 15.6 s |
| Turns | 6 | 5 | 5 | 4 | 4 | 4 |
| Browser calls | 5 | 5 | 4 | 3 | 3 | 3 |
| Input tokens | 103k | 101k | 78k | 65k | 68k | 71k |

- Both versions answered every run correctly, with no failed call.
- The differences are within run-to-run noise (li_search: 14-25 s before, 16-29 s after).
  Opus did each task with goto, read and query only: it opened search URLs directly and never clicked or typed.
  Step lists and the click and profile fixes act on clicks, typing, scripts and profile choice, so these tasks do not exercise them.
- The real outreach and Gmail runs (median 31 calls, 631 `eval` calls in 12 days) click, type and run scripts.
  Their effect shows only in live runs after north restarts on the new code.

## Results, 2026-11-22: scripts that only read need no approval

Of 631 `eval` scripts from 2026-11-10 to 11-22, 431 (68%) only read the page by their text (no click, value, focus, DOM change, navigation, request or storage); 200 changed something, 174 of them by a click.

### All 631 scripts in a headless Chrome, side-effect-free mode

A throw-away profile and a test page; each script ran with `Runtime.evaluate(throwOnSideEffect=true)`, in a block, then as a function body, then as an async function body.

- Writes: the page changed after none of the 200. 95 were stopped; the rest failed or returned early because their target was not on the test page, before they reached the write.
- Reads: 324 ran and 48 failed only because an element they looked for was not on the test page (87% not stopped); 58 were stopped.
  Stopped although harmless: a top-level `const` or `let` without the block, `closest()`, `new URL()`, `document.evaluate()`, `console.log()`, `structuredClone()`, timers and `await`.
  Chrome let a script build and fill arrays and maps of its own; Node's V8 is stricter.

### Live, Hacker News and the article chain, 3 runs each (`20261122-224640`)

| | before (`20261122-202157`, `-202413`, `-222553`) | after |
|---|---|---|
| `eval` calls | 13 | 15 |
| Ruled on by the approval model | 10 | 4 |
| Median decision time | 1,740-2,084 ms | 2 ms |

Of the 4 still ruled on, 2 named no script (a wrong parameter name) and 2 were Wikipedia reads that Chrome stopped on a harmless call; each was asked about as before.
