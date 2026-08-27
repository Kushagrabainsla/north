# North Web Interface Design

Status: Proposed

## 1. Decision

Add a browser-based presentation layer to North while preserving the existing
FastAPI backend, CLI commands, and Textual TUI.

During migration:

```text
Web application ----+
Textual TUI ---------+---- Orchestrator REST API and SSE ---- North runtime
CLI commands --------+
```

The web application becomes the primary product interface after it reaches
feature parity. The TUI remains available until a separate parity review says
it can be retired. Non-interactive CLI commands remain permanently for server
management, scripting, debugging, and recovery.

## 2. Product principles

1. **Show state, not just conversation.** Tasks, runs, approvals, schedules,
   reports, context, and costs are first-class objects with their own views.
2. **Keep attention focused.** The home screen prioritizes items that require
   action and work currently in progress. Detailed telemetry stays one level
   deeper.
3. **Live by default, durable after refresh.** SSE updates the interface in
   real time, but every important screen can reconstruct itself from REST APIs.
4. **One backend contract.** The web app uses the same public API as the TUI and
   CLI. It must not import Python stores or bypass request protections.
5. **Progressive disclosure.** A task begins as a readable answer. Plans, agent
   runs, tool calls, model use, and raw events are available in an inspector.
6. **Local-first security.** The initial release binds to loopback and does not
   imply safe remote access.
7. **Preserve compatibility.** Web work must not change existing CLI or TUI
   behavior unless a shared API defect is being fixed.
8. **Chats are durable workspaces.** A conversation survives refreshes and
   restarts. Every prompt remains connected to its response, task, execution
   details, approvals, and artifacts.
9. **Cockpit first, verbose on demand.** Every North subsystem has a live
   summary on one dashboard and a dedicated page for complete inspection and
   control.

## 3. Information architecture

| Route | Purpose | Primary objects |
|---|---|---|
| `/` | Complete North cockpit | every subsystem, attention, activity, health |
| `/chat` | Browse previous chats or start a new one | conversations, search, drafts |
| `/chat/:conversationId` | Full persistent chat room | turns, tasks, responses, execution details |
| `/tasks` | Search and filter all work | tasks, statuses, agents, timestamps |
| `/tasks/:taskId` | Inspect one task end to end | output, plan, runs, events, artifacts |
| `/briefings` | Browse generated briefings and reports | daily news, generated documents |
| `/briefings/:date` | Read one daily briefing | rendered Markdown, sources |
| `/artifacts` | Browse every generated output | reports, files, images, exports |
| `/artifacts/:artifactId` | Preview or download one output | metadata, preview, producing task |
| `/schedule` | Manage queued and recurring work | jobs, cron entries, calendar |
| `/approvals` | Resolve items that need attention | approvals, questions, information cards |
| `/memory` | Inspect and edit North's context | context documents, imports |
| `/agents` | Understand and invoke agents | capabilities, domains, model pools |
| `/agents/:name` | Inspect one agent | accepts, runs, tools, confidence |
| `/activity` | Inspect complete system activity | events, failures, warnings, ledger records |
| `/insights` | Understand system performance | metrics, costs, models, tool confidence |
| `/system` | Inspect North runtime health | server, workspace, version, connections |
| `/settings` | Configure runtime behavior | power, autonomy, workspace, interface settings |

The ledger remains available as an advanced diagnostic view under Insights,
not as a top-level product concept.

## 4. Application shell and tabs

Every major aspect of North has a persistent application tab. On wide screens
the tabs can be shown as a left rail with labels; on narrower screens they can
be a horizontally scrollable tab bar or navigation drawer. They are routes, so
refreshing, bookmarking, and browser back/forward navigation work normally.

Primary tabs:

- Dashboard
- Chat
- Tasks
- Briefings
- Artifacts
- Schedule
- Approvals
- Memory
- Agents
- Activity
- Insights
- System
- Settings

Tabs preserve their own useful UI state, such as active filters, scroll
position, selected chat, and open detail panel. Live work continues when the
user switches tabs. An active-task indicator and pending-approval badge remain
visible from every tab.

Desktop layout:

```text
+------------------+--------------------------------------+------------------+
| north            | open workspace tabs                 | status / profile |
|                  +--------------------------------------+------------------+
| Dashboard        |                                                         |
| Chat             |                    page content                         |
| Tasks            |                                                         |
| Briefings        |                                                         |
| Artifacts        |                                                         |
| Schedule         |                                                         |
| Approvals      2 |                                                         |
| Memory           |                                                         |
| Agents           |                                                         |
| Activity         |                                                         |
| Insights         |                                                         |
| System           |                                                         |
|                  |                                                         |
| Settings         +---------------------------------------------------------+
| server: online   | command composer / active-task controls when relevant   |
+------------------+---------------------------------------------------------+
```

- The left navigation is the wide-screen form of the application tabs and
  collapses to a tab bar or drawer on narrow screens.
- The top bar contains the page title, search, connection status, and actions
  relevant to the current object.
- A global command composer can start a task from any page without forcing the
  user back into Chat.
- Pending approvals display a count in navigation and appear in the attention
  queue. They must never rely on a toast alone.
- Connection loss is visible. The UI keeps already-loaded data readable and
  identifies which live views are stale.

## 5. Core screens

### 5.1 Dashboard cockpit

The dashboard is one live cockpit page representing every aspect of North. It
is not a landing page with a few shortcuts. A user can understand the complete
state of the system without visiting another route.

It answers these questions in order:

1. What needs my attention?
2. What is North doing now?
3. What did North produce for me?
4. What will happen next?
5. Are agents, models, memory, and the server healthy?
6. What has North used or learned recently?

Layout:

```text
+----------------------+----------------------+----------------------+
| System               | Attention            | Active now           |
| server, model, mode   | approvals/questions  | chats/tasks/progress |
+----------------------+----------------------+----------------------+
| Current conversations                 | Agents                   |
| recent and running chats              | availability/activity    |
+---------------------------------------+--------------------------+
| Today's briefing                      | Schedule                 |
| lead stories and generated reports    | next jobs/recurrences    |
+---------------------------------------+--------------------------+
| Recent artifacts       | Memory/context       | Usage and cost       |
| outputs and files      | changes/health       | models/tokens/trend  |
+------------------------+----------------------+----------------------+
| Recent system activity and warnings                          View all |
+-----------------------------------------------------------------------+
```

Every cockpit panel has three levels of interaction:

1. **Glance:** status, counts, and the most important current item.
2. **Quick action:** resolve an approval, open a running chat, pause a task,
   trigger a briefing, or inspect the next scheduled job without leaving the
   cockpit when the action is unambiguous.
3. **Verbose view:** `Open`, `View all`, or clicking a record navigates to that
   subsystem's dedicated page with full data and controls.

The cockpit includes all of these panels:

- system and connection health;
- attention queue;
- active tasks and chats;
- recent conversations;
- agents and their current activity;
- today's briefing and recent reports;
- upcoming jobs and recurring schedules;
- recent artifacts;
- memory and context status;
- inference usage, model health, and cost;
- recent system events, failures, and warnings.

Panels update independently so one slow metric does not block the page. Their
layout can be rearranged or resized later, but the first release ships with a
strong default layout. Compact cards must show meaningful content, not merely
an icon and link. Charts stay small and explanatory; the cockpit should remain
an operating surface rather than become a wall of analytics.

### 5.1.1 Verbose subsystem views

Every cockpit panel maps to a dedicated page:

| Cockpit panel | Verbose page |
|---|---|
| Attention | `/approvals` |
| Active work and conversations | `/chat/:conversationId` and `/tasks` |
| Agents | `/agents` and `/agents/:name` |
| Briefing and reports | `/briefings` and `/briefings/:date` |
| Schedule | `/schedule` |
| Artifacts | `/artifacts` and `/artifacts/:artifactId` |
| Memory and context | `/memory` |
| Usage, models, and cost | `/insights` |
| System activity and warnings | `/activity` |
| System state | `/system` |

The dashboard and verbose pages share the same query definitions and event
state. A number shown in the cockpit must match the corresponding detailed
page; the interface must not maintain two competing interpretations of North's
state. Verbose pages provide the full history, search, filters, sorting,
pagination, metadata, and subsystem-specific controls that do not fit safely
inside cockpit cards.

### 5.2 Chat

Chat is a complete, persistent chat room. It remains the fastest way to ask
North for something, but it does not carry the whole interface.

```text
+----------------------+--------------------------------+--------------------+
| Previous chats       | Current chat                   | Chat details       |
| Search               |                                | participants       |
| + New chat           | Prompt                         | chat cost          |
|                      | Response                       | artifacts          |
| Today                | [expand execution details]     | context            |
|  North web design    |                                |                    |
|  Daily briefing      | Prompt                         |                    |
| Yesterday            | Response streaming...          |                    |
|  Refactor scheduler  | [pause] [steer] [cancel]       |                    |
+----------------------+--------------------------------+--------------------+
|                      | sticky composer                                     |
+----------------------+-----------------------------------------------------+
```

#### Previous chats

- A searchable chat rail shows all previous conversations grouped by recency.
- Each item shows its title, latest activity, current state, and whether work is
  still running.
- Chats can be renamed, pinned, archived, and restored.
- Starting a new chat creates an empty durable conversation immediately.
- Selecting a previous chat restores the complete room, not merely its last
  response.
- Infinite pagination loads older chats without making initial startup depend
  on the complete history.

#### Full chat room

- The middle column shows every turn in chronological order.
- The current in-progress turn streams in place while previous turns remain
  readable and interactive.
- A sticky composer stays at the bottom of the room.
- Switching tabs or chats does not cancel active work.
- Returning to a running chat reconnects to its stream and restores durable
  state before applying new live events.
- The chat header provides rename, archive, export, and chat-level search.
- Generated artifacts also appear in the optional chat details panel.

#### Prompt execution bundles

Every user prompt starts one root task and forms a self-contained turn bundle.
The bundle keeps all information produced because of that prompt physically
close to the prompt and response.

Collapsed, a completed bundle shows:

- prompt;
- final response;
- status, duration, agents, and cost summary;
- artifact count and any unresolved attention item.

Expanded, it can show:

- classification and routing decision;
- execution plan and progress;
- agent run tree and attempts;
- tools called, inputs, outputs, and failures;
- selected skills;
- models, tokens, latency, and cost;
- approval and question cards;
- verification and Definition-of-Done results;
- generated artifacts;
- errors, retries, and repair activity;
- complete event timeline.

Each section inside a turn can expand or collapse independently. The room also
provides `Expand all details` and `Collapse all details`. Expansion state is a
presentation preference and does not alter task state. Details default to
collapsed for completed turns, while the currently relevant stage of an active
turn opens automatically.

Task controls belong to their prompt bundle. An active turn can be paused,
resumed, cancelled, or steered without affecting other turns in the chat.

### 5.3 Tasks

The task index supports status, agent, source, and date filters plus text
search. Each row shows the prompt, current state, agents, duration, cost, and
last update.

Task detail has four tabs:

- **Result:** final answer and artifacts.
- **Progress:** plan and current execution state.
- **Runs:** agent execution tree with attempts, models, tokens, cost, and errors.
- **Timeline:** durable events and ledger entries for debugging.

### 5.4 Briefings and artifacts

Briefings are a library, not chat messages.

- Default to today's daily briefing.
- Provide date navigation and an archive list.
- Render Markdown with a readable article width.
- Open external sources in a new tab and visibly show their domain.
- Preserve access to the raw Markdown file.
- Later, generalize this section to all artifacts: reports, plans, exported
  files, screenshots, and generated media.

The browser must never receive an arbitrary filesystem path. The backend maps
allowed North-owned outputs to opaque artifact IDs.

### 5.5 Schedule

Use two coordinated views:

- **Agenda:** upcoming queued jobs in chronological order.
- **Recurring:** human-readable recurrence cards with enable, edit, and delete.

A calendar view is useful after the underlying API represents time zones and
recurrence consistently. It is not required for the first slice.

### 5.6 Approvals

- Pending items appear first and remain visible until resolved.
- Approval cards show the requesting agent, consequence, exact proposed action,
  and relevant diff or command.
- Question cards support options and free-form answers.
- Resolving a card updates all open tabs through the global stream.
- Resolved history is searchable and visually distinct from pending work.
- Destructive decisions require a deliberate action and cannot use optimistic
  UI updates.

### 5.7 Memory

- Show each context document as a separate section.
- Provide rendered and editable modes.
- Support importing text, URL, or file using the existing context endpoint.
- Warn clearly that saving overwrites the complete document.
- Add version history before introducing automatic inline editing.

### 5.8 Agents and Insights

The Agents index shows what each agent handles and which model pool it uses.
Agent detail combines recent runs and tool-confidence data.

Insights combines operational information that is valuable but not part of the
daily workflow:

- task throughput and completion rate;
- cost by period, model, and component;
- model-pool availability;
- tool confidence;
- searchable ledger diagnostics.

## 6. Visual language

North should feel like a calm operating surface rather than a generic admin
template.

- Dense enough for serious work, with generous space around reading content.
- Neutral base colors with one restrained accent color.
- Semantic colors are reserved for running, success, warning, failure, and
  approval states.
- Typography distinguishes prose, structured metadata, and code without using
  excessive card borders.
- Motion is limited to state transitions, streaming, and connection feedback.
- Light and dark themes share the same semantic design tokens.
- Keyboard navigation and visible focus states are required from the first
  release.

## 7. Frontend architecture

Recommended implementation:

- React and TypeScript for the stateful, multi-view client.
- Vite for development and production builds.
- A query cache for REST server state. Prefer TanStack Query or a similarly
  focused library instead of a global application store for all data.
- React Router for URL-addressable objects and browser navigation.
- A small event reducer for SSE updates keyed by `task_id` and `run_id`.
- Sanitized Markdown rendering with explicit handling for links, code, tables,
  and diffs.
- Component tests for stateful controls and browser tests for critical flows.

The production build is static and is served by FastAPI under `/app`. There is
no Node process in production. The API remains under `/orchestrator`, avoiding
CORS and separate deployment concerns.

Suggested repository structure:

```text
web/
  package.json
  vite.config.ts
  src/
    app/
    api/
    components/
    features/
      approvals/
      agents/
      briefings/
      chat/
      memory/
      schedule/
      tasks/
    styles/
  tests/
  dist/                 # generated production assets
```

Feature folders own their API queries, event adapters, components, and tests.
Shared components contain presentation behavior, not North domain rules.

## 8. Local access and request security

The CLI continues authenticating with `X-North-Secret`.

The local web interface has no sign-in screen. Opening `/app` while North is
running goes directly to the product. The user is never asked to find, paste,
or manage North's secret.

North still protects browser requests silently. A website open in another tab
can attempt requests to services on localhost, so "no login" must not mean
"accept every browser request." For the browser:

1. FastAPI serves the application and API from the same loopback origin.
2. Loading the application automatically creates an HttpOnly,
   SameSite=Strict local session. This is invisible to the user and is not an
   identity login.
3. The master North secret is never exposed to browser JavaScript.
4. Mutating requests require a per-session anti-CSRF token plus valid
   same-origin `Origin` and `Host` headers.
5. CORS stays disabled and accepted hosts are limited to loopback names.
6. Native EventSource uses the automatically issued local session cookie.

The first release binds to `127.0.0.1`. Listening on a LAN or public interface
is a separate mode requiring explicit authentication, TLS, origin
configuration, stronger session lifecycle controls, and a threat-model review.

Rendered Markdown must sanitize raw HTML. Artifact access must be allowlisted
and path traversal tested. Approval responses remain bound to server-issued
card IDs, as they are today.

## 9. Existing API coverage

The current API already supports:

- task submission, status, cancellation, pause, resume, and steering;
- per-task and global SSE streams;
- ledger queries and search;
- agent discovery and direct invocation;
- context reading, writing, and import;
- jobs and recurring schedules;
- cost summaries, model pools, metrics, and tool confidence;
- task run trees and durable run events;
- settings updates and approval responses.

These contracts should remain compatible with current CLI and TUI clients.

## 10. Required backend additions

The web interface exposes several missing product-level contracts:

1. **Silent local sessions:** automatic loopback session issuance, strict host
   and origin validation, and CSRF protection with no user-facing login.
2. **Durable conversations:** first-class conversation and turn records. A
   conversation owns an ordered set of turns; each user prompt creates a turn
   linked to its root task. Titles, pin/archive state, timestamps, and drafts
   survive restarts.
3. **Conversation APIs:** paginated list and search, create, read, rename,
   archive/restore, export, append prompt, and paginated turn retrieval. The
   server, not the browser, constructs prior-conversation context for follow-up
   prompts.
4. **Task-to-chat association:** add `conversation_id` and `turn_id` to task
   submission and durable task state without changing callers that omit them.
5. **Task history:** a paginated task-summary endpoint. The UI should not infer
   task objects by grouping raw ledger rows.
6. **Complete task detail:** prompt, status, final output, timestamps, agents,
   source, cost, and artifact references in one stable response.
7. **Approval listing:** pending and recent cards through a protected GET
   endpoint. SSE alone cannot reconstruct state after a refresh.
8. **Briefings:** list available dates and read a selected briefing through a
   safe endpoint.
9. **Artifact registry:** opaque IDs, media type, size, created time, producer,
   task association, preview, and download.
10. **Schedule updates:** edit and enable/disable operations, not just create and
   delete.
11. **Resumable events:** monotonic event IDs plus `Last-Event-ID` support, or an
   equivalent cursor, so reconnects neither lose nor duplicate state.
12. **Server metadata:** version, workspace, uptime, and capability flags for the
   status surface.
13. **Interactive-client presence:** generalize TUI-specific connection state so
    notification behavior is correct when a web client is active.

Add these as API capabilities before their corresponding screens. Do not add
web-only routes that query private stores directly.

### 10.1 Conversation records

The durable relationship is:

```text
Conversation
  id, title, pinned, archived, created_at, updated_at
  |
  +-- Turn 1
  |     id, position, prompt, root_task_id, status, timestamps
  |     +-- final response
  |     +-- task detail and agent runs
  |     +-- approvals and questions
  |     +-- artifacts
  |
  +-- Turn 2
        ...
```

A conversation is not an agent transcript copied into one text field. Turns
reference durable task and artifact records so each expandable section can be
loaded independently. A lightweight turn summary supports initial chat-room
rendering; opening a detail section fetches its richer data on demand.

The existing TUI remains backward compatible: task submissions without a
`conversation_id` continue to work as they do today. The web client always
submits through a conversation, and the backend owns ordering and context
assembly to prevent two open tabs from assigning conflicting turn positions.

## 11. Delivery plan

### Phase 0: Foundation

- Create the web package, build pipeline, FastAPI static mount, and silent local
  session flow.
- Generate or hand-maintain a typed API client from the FastAPI schema.
- Add shared layout, error handling, connection state, and accessibility checks.
- Keep the new interface behind an explicit `north web` or development route.

### Phase 1: First useful vertical slice

- Dashboard cockpit shell with system, attention, active work, and recent chats
- Durable conversation creation and previous-chat list
- Full chat room and task submission
- Per-prompt expandable execution bundles
- Global and per-task streaming
- Active tasks
- Task result and basic progress
- Pause, resume, cancel, and steer

This phase proves the API, silent local-session security, SSE reducer, and
packaging choices.

### Phase 2: Daily operating surface

- Approval inbox
- Briefing library
- Task history and run inspector
- Jobs and recurring schedules
- Artifact library and cards
- Corresponding cockpit panels for every Phase 2 subsystem

At the end of this phase the web interface should be useful as North's default
daily surface, while the TUI remains the fallback.

### Phase 3: System management

- Memory and context editing
- Agents and direct invocation
- Insights, models, costs, and confidence
- Activity and system-health views
- Settings
- Remaining cockpit panels for memory, agents, insights, and settings
- Responsive layouts and keyboard command palette

### Phase 4: Parity and retirement review

- Compare every TUI action and slash command against an equivalent web or CLI
  path.
- Run both clients against the same API contract tests.
- Exercise restart, reconnect, approval timeout, and multi-tab behavior.
- Mark the web interface as default only after critical parity is complete.
- Retire the TUI in a later release, with a documented fallback to CLI commands.

## 12. TUI retirement criteria

The TUI can be retired when all of the following are true:

- Chat streams reliably and reconnects without corrupting responses.
- Previous chats and complete turn history survive refreshes and server
  restarts.
- Every prompt retains its response, execution details, approvals, and
  artifacts as one expandable bundle.
- Active, completed, paused, failed, and queued tasks are visible.
- Approval and question workflows work after refresh and across multiple tabs.
- Plans, tools, runs, errors, and final outputs are inspectable.
- Dictation, steering, cancellation, and settings have replacements.
- Briefings, generated artifacts, schedules, and context are accessible.
- The cockpit represents every subsystem and each panel opens a complete
  verbose page with matching state.
- The web build ships with normal North installation and update flows.
- Browser end-to-end tests cover the critical workflows.
- The non-interactive CLI remains operational if the web application fails.

## 13. First implementation boundary

The first implementation should stop after this end-to-end flow works:

1. Run North locally.
2. Open `/app` directly with no authentication prompt.
3. Submit a prompt.
4. See its status and streamed answer.
5. Expand and collapse its plan, agents, tools, and timeline around that turn.
6. Pause, resume, steer, or cancel it.
7. Start another chat and switch between both rooms.
8. Refresh the page and recover both chat history and durable task state.

This boundary delivers a real product slice without prematurely implementing
every dashboard screen.
