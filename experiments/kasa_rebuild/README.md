# One-prompt Kasa rebuild

Step 7 of the plan to let north rebuild the Kasa tool and its page from one prompt. Steps 2 to 6
(PRs #106 to #109) gave north tool environments, `request_secret`, custom panels and the guides, and
deleted the built-in Kasa tool and the Devices page. This test asks whether north can now make both
again from one prompt, with no help:

> Add my Kasa lights: a tool to control them, and a panel on the Views page with a switch, brightness
> and colour for each light, and music sync.

Runs 4 to 6 add one sentence, so that north tests only two of the four lights: "Test it only on Upper
desk lamp and Desk lamp."

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

| | Run 1 | Run 2 (failed) | Run 2 (again) | Run 3 | Run 4 | Run 5 | Run 6 |
|---|---|---|---|---|---|---|---|
| Date (2026-11-) | 25 | 26 | 26 | 26 | 26 | 26 | 26 |
| Tool made | yes | **no** | yes | yes | yes | yes | yes |
| Panel added, all 4 parts | yes | **no** | yes | yes | yes | yes | yes |
| Panel has a white control | no | - | no | no | yes | yes | yes |
| Ended by itself | yes | yes | yes | yes | **no** | yes | yes |
| Minutes | 2.8 | 1.3 | 1.1 | 1.3 | 20.2 (stopped) | 7.3 | 2.4 |
| Cost (USD) | 0.70 | 0.43 | 0.46 | 0.39 | 2.25 | 1.07 | 0.81 |
| Model turns | 19 | 13 | 10 | 8 | 72 | 37 | 13 |
| `create_tool` calls | 6 | 6 | 2 | 2 | 18 | 9 | 2 |
| Meta calls failed | 3 of 9 | 5 of 8 | 0 of 5 | 0 of 5 | 1 of 21 | 0 of 12 | 0 of 5 |
| Cards (all auto-answered) | 8 | 7 | 4 | 5 | 58 | 27 | 9 |
| Panel showed the lights | after a fix | - | 4 of 4 | 4 of 4 | 4 of 4, not on every read | 4 of 4 | 4 of 4 |

Model: Sonnet 5.5 in runs 1 to 5, Opus 5.5 in run 6. Each run pinned `python-kasa==0.12.0` in the tool's own environment,
used the saved Kasa secrets (no `request_secret` card) and made `list` read-only, so the panel can read
it. Run 4 was stopped, so north saved no cost for it; its cost is the sum of its ledger rows. Files:
`results/run1.json`, `results/run2_deleted_folder.json`, `results/run2.json`, `results/run3.json`,
`results/run4.json`, `results/run5.json`, `results/run6.json`.

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
   The main cause was in north. A run kept the tool object it called first, so after each update the
   task's own calls still went to the old version: the process that served them started at 17:12:30
   UTC with the second version, and none of the 16 later updates stopped it. No fix north made reached
   its own tests, so it kept rewriting. That old process also started a second copy of the daemon.
   Fixed in PR #115: each turn takes the registry's current version of every tool the run has.
6. **The desk lamps stall after any change.** A test through the daemon, with one daemon and no other
   client on the Mac: 15 reads in a row worked; after one `set` to the brightness the lamp already had,
   the next read took 60 seconds, and fresh logins also failed in that time. Stalls lasted 20 to 60
   seconds. Only the two desk lamps (the newer KLAP protocol) do this. The cause is not known (firmware,
   or another client such as the Kasa phone app); north cannot fix it.
7. **Run 5: north stopped by itself.** With #114 and #115 on main, north made the tool and the panel with
   the White slider, then kept changing the tool to handle the lamp stall (9 `create_tool` calls, 6
   tool-change cards: retries, and a direct query of a light that discovery missed). After each update, the task's next call started a new
   process within 2 seconds, so each fix reached north's own tests, and only one runner was alive at
   the end. Its last 11 tool calls worked. It tested brightness, colour, power and white on the two desk
   lamps only, put both back to warm white at 2700 K and 100 %, and said that it did not test music
   sync or the panel in a browser. Lamp1 and Lamp2 were not changed.
8. **Run 6: north reported the lamp stall and did not change its code.** PR #118 added one general rule
   to the `adding-a-home-device` guide: a device that answers on some calls and not on others does not
   have broken code; report it once, with what failed and how often. North made the tool and the panel
   with the White slider in 2 `create_tool` calls (run 3: 2, run 5: 9), 13 model turns (run 3: 8, run
   5: 37), 2.4 minutes (run 3: 1.3, run 5: 7.3) and $0.81 (run 3: $0.39, run 5: $1.07). 2 of its 8
   `kasa_lights` calls failed with a login error. Its answer said that the Upper desk lamp failed 2 of 5
   calls and worked again about 20 seconds later, that the cause looks like the lights and not the code,
   and that it did not change the tool. It tested brightness on the Upper desk lamp and colour on the
   Desk lamp, put both back to 100 % warm white, and said that it did not test music sync. Lamp1 and
   Lamp2 were not changed: a read before and after the run gave the same state. No gap between the
   task's events was 90 seconds or longer, so the `create_tool` hang fixed in #120 did not occur. The
   model also changed: run 6 used Opus 5.5, runs 1 to 5 Sonnet 5.5. A turn cost more ($0.062 against
   $0.029 in run 5), so the cost fell less than the turns.

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

- One prompt, one user, one home network, 6 runs (7 with the failed one). The cost and turn numbers
  vary from run to run; they are not a benchmark.
- Run 6 changed two things at once: the guide rule (#118) and the model (Opus 5.5, not Sonnet 5.5). One
  run cannot show how much of the fall from 37 to 13 turns each change caused.
- Music sync was not tested: it needs the browser microphone and music playing.
- Runs 4 and 5 tested the guide change; run 5 tested the stale-tool fix once. Two runs show that north
  can follow the new recipe, not that it always will.
- Run 5 cost more than run 3 ($1.07 against $0.39) because of the lamp stall: north spent its extra
  turns on retries for a device behaviour, not on a fault in its code.
