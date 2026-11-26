# One-prompt Kasa rebuild

Step 7 of the plan to let north rebuild the Kasa tool and its page from one prompt. Steps 2 to 6
(PRs #106 to #109) gave north tool environments, `request_secret`, custom panels and the guides, and
deleted the built-in Kasa tool and the Devices page. This test asks whether north can now make both
again from one prompt, with no help:

> Add my Kasa lights: a tool to control them, and a panel on the Views page with a switch, brightness
> and colour for each light, and music sync.

Run 4 adds one sentence, so that north tests only two of the four lights: "Test it only on Upper desk
lamp and Desk lamp."

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

| | Run 1 | Run 2 (failed) | Run 2 (again) | Run 3 | Run 4 |
|---|---|---|---|---|---|
| Date (2026-11-) | 25 | 26 | 26 | 26 | 26 |
| Tool made | yes | **no** | yes | yes | yes |
| Panel added, all 4 parts | yes | **no** | yes | yes | yes |
| Panel has a white control | no | - | no | no | yes |
| Minutes | 2.8 | 1.3 | 1.1 | 1.3 | 20.2 (stopped) |
| Cost (USD) | 0.70 | 0.43 | 0.46 | 0.39 | 2.25 |
| Model turns | 19 | 13 | 10 | 8 | 72 |
| Meta calls failed | 3 of 9 | 5 of 8 | 0 of 5 | 0 of 5 | 1 of 21 |
| Cards (all auto-answered) | 8 | 7 | 4 | 5 | 58 |
| Panel showed the lights | after a fix | - | 4 of 4 | 4 of 4 | 4 of 4, not on every read |

Model: Sonnet 5.5 in every run. Each run pinned `python-kasa==0.12.0` in the tool's own environment,
used the saved Kasa secrets (no `request_secret` card) and made `list` read-only, so the panel can read
it. Run 4 was stopped, so north saved no cost for it; its cost is the sum of its ledger rows. Files:
`results/run1.json`, `results/run2_deleted_folder.json`, `results/run2.json`, `results/run3.json`,
`results/run4.json`.

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
4. **Runs 1 to 3: the panel had no white control.** After a colour change, the panel could not make a
   light white again, though the tool takes `kelvin`. The `adding-a-home-device` guide caused it: its
   panel recipe and its Hue example had no white control, and north copied the example. The guide now
   asks for a control for every setting `set` takes, and its example has a White slider. In run 4,
   north made the panel with a White slider (2500 to 6500 K) in the first minute.
5. **Run 4: north did not stop testing.** A `set` on a desk lamp failed now and then ("No Kasa light
   answered"). North rewrote the tool 18 times to fix it, added retries and pauses, and then a
   background daemon. The run was stopped after 20 minutes. The tool it left is 395 lines (run 3: 237).
   Two copies of its daemon ran at the same time. The desk lamps keep one login session, so each login
   can break the other's. With one daemon, a desk lamp still gave decrypt errors for some seconds after
   each change. The cause is not proven. Two runner processes of the tool were also alive, from 09:12
   and 09:30, though `create_tool` closes the old runner on an update. This is not explained yet.

## Panel click

A click on a switch in the panel changed a real light, and a second click changed it back. The tool
answered "ok" both times, and the next read showed the new state.

The first click reached a different light than the one intended. The tool lists the lights in the order
discovery finds them, which changes on every read, and the panel reads every 30 seconds. Three reads in
a row gave three different orders. A list that moves under the pointer can make a person click the
wrong light too. Discovery also missed one of the 4 lights in about 1 read of 5. Fixed in PR #113: the
panel keeps each row in its first place, by the item's `id`.

Brightness and colour, run 3's panel, on Lamp1: brightness 40 to 81 %, then colour to hue 119 at
saturation 100. Both calls answered "ok", and the next read showed the new values. The panel could not
make the light white again (finding 4).

Run 4's panel, on the two desk lamps only. Each click sent one call, and each call answered "ok":

| Light | Control | Sent | The next good read showed |
|---|---|---|---|
| Upper desk lamp | Colour | hue 119 | hue 119, saturation 100 |
| Upper desk lamp | White | 2694 K | 2694 K, white |
| Desk lamp | Brightness | 60 %, then 100 % | 60 %, then 100 % |

The first White call failed while two daemons ran (finding 5); it worked after both were stopped. Some
reads after a change missed the changed light, so "the next good read" is the first read that had it.

## Limitations

- One prompt, one user, one home network, 4 runs (5 with the failed one). The cost and turn numbers
  vary from run to run; they are not a benchmark.
- Music sync was not tested: it needs the browser microphone and music playing.
- Run 4 tested the guide change once. One run shows that north can follow the new recipe, not that it
  always will.
