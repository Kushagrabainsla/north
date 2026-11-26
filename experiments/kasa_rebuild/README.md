# One-prompt Kasa rebuild

Step 7 of the plan to let north rebuild the Kasa tool and its page from one prompt. Steps 2 to 6
(PRs #106 to #109) gave north tool environments, `request_secret`, custom panels and the guides, and
deleted the built-in Kasa tool and the Devices page. This test asks whether north can now make both
again from one prompt, with no help:

> Add my Kasa lights: a tool to control them, and a panel on the Views page with a switch, brightness
> and colour for each light, and music sync.

## Method

- The prompt is sent in a new dashboard session, on the live north, with real bulbs.
- North runs in Autonomous mode, so north answers its own cards. That is intended.
- Between runs, the tool and the panel are deleted on the dashboard (Tools page, Views page).
- `state.py` lists what north's home holds for Kasa: secret names, learned tool files, views and
  environments. It shows names only.
- `measure.py <label> [<task_id>]` reads north's home read-only and writes `results/<label>.json`.
  The file holds counts, names and booleans only: no prompt text, no secret value, no device data.

```bash
.venv/bin/python experiments/kasa_rebuild/state.py
.venv/bin/python experiments/kasa_rebuild/measure.py run3 task_72f127e2d234
```

## Results

| | Run 1 | Run 2 (failed) | Run 2 (again) | Run 3 |
|---|---|---|---|---|
| Date (2026-11-) | 25 | 26 | 26 | 26 |
| Tool made | yes | **no** | yes | yes |
| Panel added, all 4 parts | yes | **no** | yes | yes |
| Minutes | 2.8 | 1.3 | 1.1 | 1.3 |
| Cost (USD) | 0.70 | 0.43 | 0.46 | 0.39 |
| Model turns | 19 | 13 | 10 | 8 |
| Meta calls failed | 3 of 9 | 5 of 8 | 0 of 5 | 0 of 5 |
| Cards (all auto-answered) | 8 | 7 | 4 | 5 |
| Panel showed the lights | after a fix | - | 4 of 4 | 4 of 4 |

Model: Sonnet 5.5 in every run. Each run pinned `python-kasa==0.12.0` in the tool's own environment,
used the saved Kasa secrets (no `request_secret` card) and made `list` read-only, so the panel can read
it. Files: `results/run1.json`, `results/run2_deleted_folder.json`, `results/run2.json`,
`results/run3.json`.

## What each run found

Each failure was in north, not in what the model wrote.

1. **Run 1: the lights gave "No route to host".** macOS Local Network privacy blocked the processes of
   north's server after a restart that no allowed app owned. A start from Terminal fixed it, and macOS
   now holds an Allow rule for north's Python.
2. **Run 1, cleanup: a removed tool stayed in north until a restart.** The registry never dropped a
   tool north wrote, and `create_tool` had no way to remove one. Fixed in PR #110: `forget_learned`,
   `create_tool action='remove'`, and the dashboard delete removes the code, draft, environment,
   own folder and process. The cleanup before run 3 used it: nothing was left, with no restart.
3. **Run 2: "uv could not make the environment: Current directory does not exist."** The dashboard's
   Update runs in site-packages, which `uv tool install --force` deletes and makes again. The new
   server inherited the deleted folder. Fixed in PR #111: the server starts in north's code folder.

## Panel click

A click on a switch in the panel changed a real light, and a second click changed it back. The tool
answered "ok" both times, and the next read showed the new state.

The first click reached a different light than the one intended. The tool lists the lights in the order
discovery finds them, which changes on every read, and the panel reads every 30 seconds. Three reads in
a row gave three different orders. A list that moves under the pointer can make a person click the
wrong light too. Discovery also missed one of the 4 lights in about 1 read of 5.

## Limitations

- One prompt, one user, one home network, 3 runs (4 with the failed one). The cost and turn numbers
  vary from run to run; they are not a benchmark.
- Music sync was not tested: it needs the browser microphone and music playing.
- Brightness and colour were not clicked; the switch was.
