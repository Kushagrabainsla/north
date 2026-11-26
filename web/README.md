# web

This package is the north dashboard. It has two halves: FastAPI routes in `web/*.py`, and a React front end in
`web/src/`. Vite builds the front end into `web/dist/`, which ships as package data. The north server mounts
`web/dist` at `/app` and serves the routes under `/web` and `/web/api`.

## Main parts: the Python API

| File | Key names | What it does |
|---|---|---|
| `api.py` | `router` (`/web/api`), `session_router` (`/web`), `configure`, `WebRuntime` | The main routes: sessions and turns, tasks, notices, approvals, memory, unattended rules, providers, Telegram, files (artifacts), system facts and the Home page data. It includes the routers of the files below. |
| `extensions.py` | `router`, `start_flow_run`, `FlowRunRequest` | Skills, tools, flow definitions, flow runs and schedules, setup, and browser profiles. |
| `views.py` | `router` | The Views page: opens a view from `views/`, reads its data, and runs a part's flow or tool call for the user. |
| `secrets_panel.py` | `router` | The Secrets panel. It lists names and notes, and adds, replaces and removes values. No route returns a value. |
| `updates.py` | `router`, `begin_update` | The Update button on the System page. It runs `north update --yes` as its own process. |
| `conversations.py` | `ConversationStore`, `Conversation`, `Turn` | Durable sessions and their turns, in SQLite, shared by every interface. |
| `browser_keys.py` | `remember_browser`, `known_browser`, `BROWSER_KEY_COOKIE` | Remembers the browsers that signed in, as hashed keys. |
| `artifacts.py` | `artifact_media_type` and other pure functions | The path rules for which files the Files page may show. |

## Main parts: the front end

| Path | What it does |
|---|---|
| `src/main.tsx`, `src/App.tsx` | Start a browser session (`bootstrapSession`), set the display time zone, and define the routes (`HashRouter`). |
| `src/kit.tsx`, `src/kit.css` | The UI kit: `Button`, `ActionBar`, `Tabs`, `Toolbar`, `EntityTable`, `ListRow`, `CardGrid`, `EntityCard`, `EntityForm`, `Field`, `Notice`, `Status`, `Badge`, `ResourceView`, `confirmDelete` and more. |
| `src/components.tsx` | The app shell (`Layout`, the sidebar), time formatting (`formatDateTime`, `timeAgo`), `Markdown`, `Modal`, and shared panels. It re-exports some kit parts. |
| `src/views.tsx` | Draws a view that north made (A2UI v0.9 messages) with kit parts. |
| `src/api.ts`, `src/hooks.ts` | `fetch` helpers with the CSRF header, and `useResource` for polled data. |
| `src/pages/` | One file per page: `Dashboard` (Home), `Approvals` (the Inbox), `Chat` (Sessions), `Tasks`, `Flows`, `Artifacts` (Files), `Memory`, `Skills`, `Tools`, `Views`, `System`, `Settings`, `Setup`. Pure logic sits beside them (`flowsView.ts`, `liveSteps.ts`). |
| Other `src/*.ts(x)` | The command palette, dialogs, notices, the Inbox count, schedule forms, connections, browser profiles, and music sync for lights (`listen.ts`, `music.ts`). |
| `src/*.test.ts`, `src/pages/*.test.ts` | Vitest tests. |

## Build and run

- Build: `npm run build` in `web/` (`tsc -b && vite build`). The output goes to `web/dist/`.
- Develop: `npm run dev` starts Vite on port 5173. It sends `/web`, `/orchestrator` and `/health` to the server
  on `127.0.0.1:8000`.
- Test: `npm test` (`vitest run`). The Python routes have tests in `tests/unit/web/`.
- `north web` rebuilds `web/dist` when it is older than its sources (`cli/web_build.py`), and then opens a one-time
  sign-in link.

## How north uses it

- `orchestrator/app.py` calls `configure_web(...)` (`web.api.configure`) to give the routes their services,
  includes `session_router` and `router`, and mounts `web/dist` at `/app` with `StaticFiles`.
- `orchestrator/app.py` also uses `ConversationStore` (`web.db`) and `begin_update`.
- Routes reach their services through `orchestrator.api_context.current_services()`, never through module state.
- `pyproject.toml` (`[tool.setuptools.package-data]`, key `web`) ships the files in `dist/` and leaves out `src`
  and `node_modules`.

## Data on disk

In north's home: `web.db` (sessions and turns), `web_access.db` (hashed browser keys), `views/` (see
`views/README.md`), and `update.log` and `update.json` (the last update).

## Rules

- Build every page from `src/kit.tsx`. Style the kit only in `kit.css`, with classes that start with `kit-`. A
  page adds no list, row, button, tab, form, notice or status style of its own. If a page needs a part that the kit
  does not have, add it to the kit (CODING_STYLE §5.1, "One UI kit").
- Use the kit mapping in CODING_STYLE §5.1: `primary` for the one main action, a red `danger` Delete that asks
  through `confirmDelete`, Cancel before Save, `Notice` with `error` for failures.
- If a view should use a new kit part, add it to `views/catalog.py` and to `src/views.tsx` together.
- Give each concept one home page. Other pages link to it or reuse its component (CODING_STYLE §5.1).
- Show a file with "Show in Finder". Never open it (CODING_STYLE §5.1).
- Use one row of tabs per page and plain words in labels (CODING_STYLE §5.1).
- Show times in the zone on the Settings page, with `formatDateTime` and `timeAgo` (CODING_STYLE §5.2.1).
- Protect every `/web/api` route with `verify_api_access`: the CLI secret or a local browser session
  (CODING_STYLE §12.3). `POST /web/session` refuses a request whose host is not local (CODING_STYLE §12.6).
- A control that the user operates acts at once with no card. A view's tool call that sends, spends, deletes,
  pushes, runs a command or changes north shows its exact values first (CODING_STYLE §7.3).
- Never send a secret value to the browser (CODING_STYLE §7.6).
