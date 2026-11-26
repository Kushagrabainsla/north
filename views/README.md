# views

This package holds the custom panels that north makes for itself on the dashboard. A view is an A2UI v0.9 JSON
document that uses only the parts in `views/catalog.py`. The package checks a view, saves it as a draft, and moves
it onto the Views page when the user approves the card. The dashboard draws each part with the UI kit part of the
same role, in `web/src/views.tsx`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `catalog.py` | `COMPONENTS`, `CONTAINERS`, `INPUTS`, `CATALOG_ID`, `A2UI_VERSION`, `component_schema`, `catalog_document`, `describe_catalog` | The allowed parts and the JSON Schema of each. The catalog id is `north:kit/v1`. |
| `document.py` | `VIEW_SCHEMA`, `check_view`, `reference_checks`, `action_tool_check`, `messages`, `summary`, `SLUG` | The view document: its schema, the full check, the A2UI messages for the renderer, and the text for the approval card. |
| `store.py` | `ViewStore` | One JSON file per view. `save_draft`, `publish`, `get`, `all`, `delete`. |

A view document has an `id` (a slug), a `title`, `data`, `state`, `refresh`, `actions` and `components`:

- `data` lists the tools that the view reads when it opens. Each result is at `/<name>` in the data model.
- `state` holds the values that the inputs set, at `/state/<name>`.
- An action runs a flow (`{"flow": ..., "inputs": ...}`) or calls a tool (`{"tool": ..., "args": ...}`). An
  argument is a fixed value, or a `{"path": ...}` binding that the browser reads when the user uses the part.
- `components` is a flat list. One component has the id `root`. Containers (`Column`, `Row`, `Panel`, `StatGrid`)
  name their children by id, or repeat one template over a list in the data.

`check_view` returns every problem in words that north can act on, or an empty list. It checks the schema, each
part, duplicate ids, a missing `root`, loops, depth (`MAX_DEPTH`), parts that `root` cannot reach, and data paths.
`reference_checks` adds checks against the live registries: a data tool must exist and must not change anything
with those arguments, and a flow must exist. `action_tool_check` checks only that an action's tool exists. The
call itself is judged when it runs.

## How north uses it

- `tools/universal/create_view.py` (`CreateViewTool`, tool name `create_view`) is how north writes views. Its
  actions are `parts`, `draft`, `add`, `list`, `read` and `remove`. Only `add` and `remove` count as changes,
  so only they go to the approval layer.
- `web/views.py` serves the Views page. It opens a view, reads its data through `Tool.execute`, and returns
  `messages(...)`. When the user uses a part, it starts the flow or calls the tool through `Tool.execute_for_you`.
- `web/src/views.tsx` draws the messages. `web/src/pages/Views.tsx` is the page.
- The skill `resources/builtin-skills/authoring-a-north-view/SKILL.md` teaches the model how to write a view.
- Public interface (`__all__`): `CATALOG_ID`, `ViewStore`, `catalog_document`, `check_view`, `describe_catalog`,
  `messages`, `summary`. Both callers also import `action_tool_check` and `reference_checks` from
  `views.document`, which are not in `__all__`.

## Data on disk

- `<NORTH_HOME>/views/<id>.json`: the live views on the Views page.
- `<NORTH_HOME>/views/drafts/<id>.json`: drafts. Only the preview link (`?draft=1`) shows a draft.

`ViewStore` writes each file to a temporary file and then renames it, so no reader sees half a view. It refuses
an id that does not match `SLUG`, because the id becomes a file name.

## Rules

- Add a part to `catalog.py` and its renderer to `web/src/views.tsx` together. A view can use only parts that the
  UI kit has (CODING_STYLE §5.1, "One UI kit").
- A view reads data only from tools that change nothing. A button or input acts only when the user uses it.
- A tool call from a view acts as the user, with no card. A call that `approval.policy.needs_your_confirmation`
  names (a send, a spend, a delete, a push, a command, a change to north) shows its exact values first and runs
  only after the user confirms that call (CODING_STYLE §7.3).
- The fixed arguments of an action stay on the server. The browser sends only the values that the action binds.
- When north adds or removes a view, the call goes through the approval layer. When the user deletes a view on the
  dashboard (`DELETE /web/api/views/{view_id}`), no card shows.
