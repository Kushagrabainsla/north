# Custom panels: before and after

Step 4 of the plan to let north rebuild the Kasa tool and its page from one prompt: a view must be a
custom panel for anything the user needs, not only a page to read.

## Real needs

North has never made a view (`~/.north/views` did not exist on 2026-11-25). So the panels come from the
user's own prompts. `find_panel_requests.py` reads every distinct chat and task prompt (205) and keeps
the 31 that ask to see, track, control or check something. Sorted by hand, the needs a panel serves:

| Need | Prompts |
|---|---|
| Screen brightness, read and set | 5 |
| LinkedIn job drafts | 6 |
| Research and news briefings | 5 |
| Recent Gmail from every profile | 1 |
| A reminder in some minutes | 1 |
| A form-filling loop every minute | 1 |

To these, the Devices page that was built by hand: Kasa lights, and music sync. The flows the user keeps
add the daily photography flashcard. That gives 10 panels, in `panels.json`. The prompts stay private
(`~/.north/experiments/view_panels/candidates.jsonl`).

## The check

`verify_before_after.py` runs each panel through one checkout's own `check_view`. Whether a tool or a
flow exists is a separate question (some panels read a tool north would make first, such as
`flow_runs`), so every tool and flow is taken to exist here.

```bash
uv run --project <main checkout> python experiments/view_panels/verify_before_after.py <main checkout> before
uv run --project . python experiments/view_panels/verify_before_after.py . after
```

| | main (442a3b8) | this branch |
|---|---|---|
| Panels accepted | **1 of 10** | **10 of 10** |

What stopped each one on main (`results/verify_before.json`):
- 6 call a tool from a part (set a level, switch a light, skip a job, add a reminder, stop a loop).
- 1 keeps an input's value (`state`): the research topic.
- 1 shows an image: the flashcard.
- 1 reads its data again on a timer (`refresh`): the email. Without `refresh`, main accepts it, but it never reads the mail again.
- Only the news briefing (read, plus a flow button) worked before.

## In a real browser

A throwaway north (empty home, port 8012) with one panel: a text field, a select, a switch, a slider, a
colour slider, a microphone, **Save note** (`write_file` into a scratch folder) and **Run echo** (`bash`).
- Every part drew in the kit's style.
- **Save note** wrote the typed note to the file at once; the store held 0 approval cards.
- **Run echo** showed `echo hello-from-panel` in a confirm box, and ran after **Do it**.
- north.log: "View panel-check: save called write_file as you (ok)", and the same for bash.

Not checked here: the Microphone with real music (it reuses music sync's analysis), and a live model
writing a panel from a request. Step 7 of the plan measures the second.
