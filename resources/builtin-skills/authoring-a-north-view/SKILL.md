---
name: authoring-a-north-view
description: "Use when the user wants a panel on north's dashboard that north makes itself: a board for a flow's runs, figures they track, their devices with switches and sliders, a form that starts some work, or any page to see and control something."
intents: [create-view]
domains: [general, engineering]
---
# Authoring a north view

> A view is a custom panel north adds to its own dashboard, on the Views page, for anything the user wants to see or control. It is drawn only from north's UI kit, so it looks like every other page. It shows data read from tools that change nothing. Its buttons and inputs run flows, or call tools as the user, because the user used them. A view never acts by itself.

## Decide first
1. Say what the panel is for and what the user will look at or do there. One panel, one purpose.
2. Find the data: a tool that changes nothing and returns what the panel shows (`query_metrics`, `create_flow` with `action='list'`, a device tool's list action). Call it once yourself to see the shape of its result; the view reads that same result at `/<name>`. No tool returns it? Make the tool first (`adding-a-north-tool`), with a read-only action.
3. Find the actions. Each one either runs a flow that exists, or calls a tool. Call the tool once yourself with the arguments the panel will send, to see that it works.
4. `create_view` with `action='parts'` lists the parts a view may use. Use only those.

## The format: A2UI v0.9 on north's kit
- `components` is a flat list. Each has an `id`, a `component` (a part from `parts`), and its properties. One has the id `root`; the panel starts there.
- Containers (`Column`, `Row`, `Panel`, `StatGrid`) name their `children` by id: `["title", "runs"]`.
- To repeat one part for each item of a list in the data, `children` is `{"path": "/lights/devices", "componentId": "light_row"}`. Inside that part, a path without the leading slash reads from the item: `{"path": "name"}`.
- A property that shows data is `{"path": "/metrics/metrics/total_tasks"}`. Text that does not change is a plain string.
- `state` holds what the panel's inputs set, with a first value for each: `"state": {"level": 50, "topic": ""}`. Read it at `/state/level`.
- An input (`TextField`, `Select`, `Switch`, `Slider`, `ColorPicker`, `Microphone`) keeps its value at its `value` path: `/state/<name>`, or a field of its row, such as `{"path": "on"}` for a light's own switch. It shows that value and writes the user's change back.
- `action` on a part runs one of the view's `actions`: a `Button` when pressed, a `TextField` on Enter, a `Switch` when turned, a `Slider` or `ColorPicker` when let go, a `Select` when picked, a `Microphone` several times a second while it listens.
- `refresh` reads the data again every so many seconds (at least 5). Data is also read again after every action.

## Actions
- **Run a flow:** `{"flow": "daily-research-briefing", "inputs": {"topics": {"path": "/state/topic"}}, "label": "Runs the briefing on your topic"}`. Its steps ask as usual.
- **Call a tool:** `{"tool": "screen_brightness", "args": {"action": "set", "brightness": {"path": "/state/level"}}, "label": "Sets the screens' brightness"}`.
- An argument is written as is, or read when the part is used with `{"path": ...}`: from `state`, the data, or the part's row (`{"path": "id"}`). Write every argument the panel never changes, so it stays as the user approved it.
- A tool call runs at once, as the user's own click. A call that sends to someone, spends, deletes, pushes, runs a command or changes north shows the user its exact values first. Add `"confirm": true` to show them for any other call that cannot be undone.
- The `Microphone` writes `{level, beat, hue, saturation, brightness}` to its `value`; bind an action's arguments to those, such as `{"path": "/state/sound/hue"}`. The sound never leaves the browser.

## A complete example (a call to `create_view`)

```json
{
  "action": "draft",
  "view": {
    "id": "screens",
    "title": "Screens",
    "description": "How bright each screen is, and a slider to change them all.",
    "data": [{"name": "screens", "tool": "screen_brightness", "args": {"action": "get"}}],
    "state": {"level": 50},
    "actions": {
      "set_level": {"tool": "screen_brightness", "args": {"action": "set", "brightness": {"path": "/state/level"}}, "label": "Sets every screen's brightness"}
    },
    "components": [
      {"id": "root", "component": "Column", "children": ["now", "control"]},
      {"id": "now", "component": "Panel", "title": "Now", "children": {"path": "/screens/screens", "componentId": "screen_row"}},
      {"id": "screen_row", "component": "ListItem", "title": {"path": "name"}, "meta": [{"path": "brightness"}]},
      {"id": "control", "component": "Panel", "title": "Set", "children": ["level"]},
      {"id": "level", "component": "Slider", "label": "Brightness", "value": {"path": "/state/level"}, "min": 0, "max": 100, "unit": "%", "action": {"event": {"name": "set_level"}}}
    ]
  }
}
```

## Check, show, add
1. `create_view` with `action='draft'` checks the view and keeps it as a draft. It names every problem: a part that does not exist, a child that is missing, a path to data or state the view does not have, a data tool that changes things, an action or a tool that does not exist. Fix each and draft again.
2. Give the user the preview link it returns, so they can open the panel before it is added. A draft's parts do nothing.
3. `create_view` with `action='add'` and the `id` puts it on the Views page. This asks the user once; the card says what the view reads and what each action does.
4. Changing a view later: draft it again with the same `id`, then add it again.

## Mistakes to avoid
- A data tool that changes things: read with a read-only action, and act with an action.
- Data copied into the view as text: it goes stale. Read it with `data` so the panel is current.
- An argument bound with `{"path": ...}` that the user should not change, such as a recipient: write it as is.
- One huge panel: split it. A view has at most 200 parts and 16 actions.
