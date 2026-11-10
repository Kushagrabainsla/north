---
name: authoring-a-north-view
description: "Use when the user wants a page on north's dashboard that north makes itself: a board for a flow's runs, figures they track, what is scheduled, or a place to see and start some work."
intents: [create-view]
domains: [general, engineering]
---
# Authoring a north view

> A view is a page north adds to its own dashboard, on the Views page. It is drawn only from north's UI kit, so it looks like every other page. It shows data read from tools that change nothing, and its buttons start flows, whose steps ask as usual. A view never acts by itself.

## Decide first
1. Say what the page is for and what the user will look at it to learn or do. One page, one purpose.
2. Find the data: a tool that changes nothing and returns what the page shows (`list_schedules`, `query_metrics`, `create_flow` with `action='list'`). Call it once yourself to see the shape of its result; the view reads that same result at `/<name>`. No tool returns it? Make the tool first (`adding-a-north-tool`), as read-only.
3. Find the actions: each button starts a flow that exists (`create_flow` with `action='list'`). No flow does it? Make the flow first (`authoring-a-north-flow`). A view has no other way to act.
4. `create_view` with `action='parts'` lists the parts a view may use. Use only those.

## The format: A2UI v0.9 on north's kit
- `components` is a flat list. Each has an `id`, a `component` (a part from `parts`), and its properties. One has the id `root`; the page starts there.
- Containers (`Column`, `Row`, `Panel`, `StatGrid`) name their `children` by id: `["title", "runs"]`.
- To repeat one part for each item of a list in the data, `children` is `{"path": "/schedules/recurring", "componentId": "schedule_row"}`. Inside that part, a path without the leading slash reads from the item: `{"path": "name"}`.
- A property that shows data is `{"path": "/metrics/metrics/total_tasks"}`. Text that does not change is a plain string.
- A `Table` lists one row per item of `rows` (a path to a list), with `columns` naming each item's keys.
- A `Button` has `"action": {"event": {"name": "run_now"}}`, and `run_now` is in the view's `actions`: `{"run_now": {"flow": "nightly-cleanup", "label": "Runs the cleanup now"}}`.
- `about` ties the view to the flow, skill or tool it is about: `{"kind": "flow", "name": "nightly-cleanup"}`.

## A complete example (a call to `create_view`)

```json
{
  "action": "draft",
  "view": {
    "id": "schedules-board",
    "title": "What runs when",
    "description": "Every schedule north keeps, soonest first.",
    "data": [{"name": "schedules", "tool": "list_schedules", "args": {}}],
    "components": [
      {"id": "root", "component": "Column", "children": ["intro", "list"]},
      {"id": "intro", "component": "Note", "text": "Times are in north's time zone."},
      {"id": "list", "component": "Panel", "title": "Schedules", "children": {"path": "/schedules/recurring", "componentId": "schedule_row"}},
      {"id": "schedule_row", "component": "ListItem", "title": {"path": "name"}, "description": {"path": "task"}, "meta": [{"path": "schedule"}, {"path": "next_run"}]}
    ]
  }
}
```

## Check, show, add
1. `create_view` with `action='draft'` checks the view and keeps it as a draft. It names every problem: a part that does not exist, a child that is missing, a path to data the view does not read, a tool that changes things. Fix each and draft again.
2. Give the user the preview link it returns, so they can open the page before it is added.
3. `create_view` with `action='add'` and the `id` puts it on the Views page. This asks the user once; the card says what the view reads and what its buttons run.
4. Changing a view later: draft it again with the same `id`, then add it again.

## Mistakes to avoid
- A data tool that changes things, or a button that should change something without a flow: make the flow.
- Data copied into the view as text: it goes stale. Read it with `data` so the page is current each time it opens.
- One huge page: split it. A view has at most 200 parts.
