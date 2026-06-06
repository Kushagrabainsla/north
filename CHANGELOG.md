# Changelog

All notable changes to north are documented here.

## [1.3.2] - 2026-06-06
### Added
- **Semantic tool selection** (`tools/tool_index.py`): tool descriptions are embedded at registration time; at task start, only the top-15 semantically relevant tools are injected into the agent context instead of the full registry, reducing token bloat at scale
- **Atomic fact store** (`context/fact_store.py`): extracted facts are stored as individual SQLite rows with per-entry embeddings; `_load_context()` retrieves the top-15 semantically relevant facts via cosine search instead of loading entire markdown documents
- **Metrics queries** (`tools/universal/query_metrics.py`, `GET /orchestrator/metrics`, `north metrics`, `/ui/metrics`): per-agent task counts, success rates, costs, p50/p95 durations, model cost breakdown, and top errors — visible via CLI, web UI, and chat
- **Delegation cycle detection**: `delegation_chain: list[str]` added to `AgentPayload`; `_delegate_task()` short-circuits with an error if the target agent is already in the chain
- **AST safety check on `create_tool`**: agent-written code is scanned for forbidden imports (`subprocess`, `socket`, `ctypes`, …) and calls (`exec`, `eval`, `compile`) before being written or hot-loaded
- **`tools_used` tracking**: every tool call in a ReAct loop is accumulated and written to `LedgerEntry.tools_used` for auditing
- **SQLite WAL mode**: `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000` applied to both the ledger and confidence databases at init
- **Adaptive EMA confidence**: consecutive failures scale the EMA alpha (0.1 → 0.2 → 0.4) for faster tool degradation detection; warning logged when confidence drops below 0.25; `consecutive_failures` column auto-migrated
- **Deterministic planner routing**: planner LLM call uses `temperature=0.0`; an in-process LRU routing cache (256 entries, 1-hour TTL) keyed on normalised prompt hash returns identical plans for recurring tasks at near-zero cost

### Changed
- `ExtractionPipeline` is now configurable: `extraction_poll_interval_seconds`, `extraction_max_daily_cost_usd`, `extraction_min_output_chars`, `extraction_max_concurrent` all settable via env vars; daily cost cap uses the new metrics endpoint; low-signal entries (short output) are skipped before the LLM call
- `Agent._load_context()` uses `FactStore` semantic search when populated, falls back to full markdown load — no change to callers
- `Agent._load_tools()` accepts `task_prompt` and applies semantic filter when `ToolIndex` is available and tool count exceeds threshold
- Dynamic planner domain routing: static routing table removed; domain values derived purely from the `=== Available Agents ===` runtime block
- `ToolRegistry.all_tools()` added for bulk enumeration at startup
- `ExecutionPlan.with_task_id()` added for safe cache replay with a new task id

## [1.3.1] - 2026-06-06
### Changed
- Resolved all ruff lint errors across modified files

## [1.2.6] - 2026-06-01
### Changed
- Resolved all 25 ruff lint errors: fixed B904 (`raise … from None`) in three API router exception handlers, suppressed B008 for `typer.Option`/`typer.Argument` via `extend-immutable-calls`, and wrapped long lines (E501) across `agents/`, `cli/`, `jobs/`, `orchestrator/`, and `tools/`

## [1.2.4] - 2026-06-01
### Added
- Four engineering agents: `researcher` (Feynman + Liskov), `architect` (Brooks + Hickey, reasoning model), `coder` (Beck + Torvalds + Uncle Bob, with `bash`/`git`/`patch_file`), `tester` (Dijkstra + Bach, full QA — writes and runs tests)
- Each agent has hard responsibility boundaries, scope-aware delegation (chains only when task scope requires it), and founding engineer principles that shape default behaviour
- Agents ask clarifying questions via `request_approval` when scope is ambiguous; answers accumulate into `judgement_rules.md` via the extraction pipeline, making the system progressively less reliant on questions over time
- `engineering` domain added to planner with entry point heuristic: "research" → researcher, "design" → architect, "build" → full chain, "fix"/"code" → coder, "test" → tester
- Task-scoped artifact layout: `.north/tasks/{task_id}/research/`, `architecture/`, `implementation/`, `qa/` — concurrent tasks never corrupt each other's files
- Tester produces versioned QA reports (`qa_report_vN.md` + `qa_report_latest.md`), detects infinite fix loops at v4+, and routes code bugs to coder vs spec problems to architect
- `produces` field added to `AgentConfig` — each agent declares its output artifacts
- `BashTool` now accepts an optional `timeout` parameter (1–300 s, default 30) — tester uses higher values for full test suites
- `_ENGINEERING_AGENTS` frozenset: delegation to engineering agents fails hard if the agent is not registered — no silent fallback to `general`
- `task_id` injected into every agent's task message so agents can construct scoped artifact paths without any additional plumbing

### Changed
- `_MAX_DELEGATION_DEPTH` raised from 2 to 10 — supports researcher→architect→coder↔tester chains with multiple fix cycles
- `delegate_task` schema description updated to list all engineering agents by name
- `AgentRegistry.get()` now triggers a live filesystem scan on cache miss — new agent folders dropped at runtime are registered on the next call, no restart required
- `ToolRegistry.tools_for_agent()` now scans the filesystem on every call — new tool files (including those written by `create_tool` mid-task) are available in the next ReAct step with no polling, no TTL, and no miss-then-retry
- Learning loop is fully system-owned: agents carry no memory-management instructions; the extraction pipeline handles all learning into `~/.north/judgement_rules.md` transparently
- `prompts/router.md` and `prompts/planner.md` examples updated — all stale "code" agent references replaced

### Removed
- Deprecated `code` agent (`agents/code/`) — replaced by the four engineering agents
- `AGENT_IMPLEMENTATION_PLAN.md` — plan is fully implemented

## [1.2.3] - 2026-06-01
### Added
- Full TUI: `north` (no subcommand) opens a single-terminal interface combining chat, live tool activity, and inline approval prompts — no separate windows needed
- Global SSE stream (`GET /stream`) mirrors all task events to a single persistent connection used by the TUI
- Terminal bell (`\a`) on task complete/fail — OS-independent notification
- Inline approval rendering: approval panels appear above the input line; macOS notifications suppressed while TUI is connected (`TUIAwareNotifier`)
- Input history saved to `~/.north/tui_history` with search support via `prompt_toolkit`
- Auto-compacting context: agent loop tracks `tokens_in` per iteration and triggers LLM summarisation of old history when it hits 40% of the model's context window
- Security gate on `create_tool`: `create_tool(action=create/update)` always triggers a `request_approval` call showing the full code before writing or hot-loading anything
- `north agent create` now updates `prompts/planner.md` automatically — newly created agents are routable immediately without a manual planner edit
- Memory deduplication: extraction pipeline checks for near-duplicate facts before appending using keyword overlap + LLM confirmation
- Memory document trimming: context documents are condensed via LLM when they exceed 8,000 chars, targeting 5,000 chars
- Improved extraction prompt: pulls structured, present-tense facts with specifics (names, numbers, dates)

### Changed
- `north start` launches the TUI after server boot instead of the old readline chat loop
- Removed `north chat` command and the underlying `_chat_loop` / `_load_history_from_ledger` / `_inject_history` functions — TUI replaces them entirely

## [1.2.2] - 2026-06-01
### Changed
- `create_tool` is now a last-resort tool — every agent's system prompt enforces a strict priority order: use existing tools first, extend a similar tool second, create a new tool only when nothing fits
- Tool description updated to lead with "last-resort" intent so the LLM filters it out during normal tool selection

## [1.2.1] - 2026-06-01
### Added
- `create_tool` universal tool: agents can create, update, read, and list north tools at runtime
  - `action=list` — discover all tools (universal + specialized) with descriptions
  - `action=read` — inspect a tool's full Python source by name
  - `action=create` — scaffold a new tool; provide full `content` for an immediately usable implementation
  - `action=update` — extend an existing tool while preserving current behaviour
- Hot-reload: newly created/updated tools are dynamically imported and registered in the running server without a restart
- In-task tool availability: agent loop refreshes the tool list from the registry at every iteration, so a tool created in step N is available for the LLM to call in step N+1 of the same task

## [1.2.0] - 2026-05-31
### Added
- Two-tier tool architecture: `tools/universal/` (auto-given to all agents) and `tools/specialized/` (opt-in per agent via `tools.yaml`)
- Auto-discovery: dropping a `.py` file into either folder is sufficient — no manual registration required
- TP-Link Kasa smart bulb control (`kasa` tool): on/off/toggle/brightness/color/color_temp with named colours and colour temperatures
- Home agent: routes smart home requests (lights, bulbs, Kasa devices) to the `kasa` tool
- `north reset` command: wipes `~/.north` data (DB, tasks, logs) for a fresh start; `--all` removes everything including `.env`
- `--docker` flag on `north start` / `north stop`: Docker is now opt-in; local mode is the default

### Changed
- Server launched as `subprocess.Popen` with stdout/stderr redirected to `~/.north/north.log` at the OS level — eliminates all log bleed into the terminal
- `north agent create` generates `tools.yaml` with the new comment format and filters out universal tools
- Planner prompt: added `home` domain row; `bash` blocked as `single_tool`
- install script: Docker check changed from hard fail to informational

### Fixed
- Kasa device discovery inside Docker (UDP broadcast conflicts with uvicorn on macOS) — fixed by running `kasa discover` as a subprocess
- Bash tool returned `success=False, error=None` when exit code was non-zero — now reports `stderr` or `exit code N`

## [1.1.9] - 2026-05-20
### Fixed
- Minor bug fixes and improvements

## [1.1.8] - 2026-05-15
### Added
- Structured JSON logging with task_id correlation IDs
- Healthcheck endpoints
- Settings caching
- Startup task sweep: orphaned PENDING tasks marked FAILED on server start
- CI workflow

## [1.1.7] - 2026-05-10
### Added
- `format_output` method on all tools for human-readable tool results
- LLM summarisation for episodic memory truncation (replaces hard truncation)

### Changed
- ApprovalStore fully migrated to dependency injection
- Extraction pipeline hardened and parallelised
- Strategy regex tightened

## [1.1.6] - 2026-05-05
### Added
- Privacy rules enforcement
- Per-task context store consolidated to single SQLite DB
- Confidence gate on north star checks
- Integration tests

### Changed
- Task and delegation caps added
- Episodic DB pruning

## [1.1.5] - 2026-04-28
### Changed
- Stable release

## [1.1.4] - 2026-04-20
### Added
- curl install script with GHCR image publishing
- Zero-config workspace mount

### Fixed
- Active tasks tracking bug

## [1.1.3] - 2026-04-15
### Fixed
- Minor bug fixes

## [1.1.2] - 2026-04-10
### Fixed
- Silent ledger write failures now surface agent errors to the user

## [1.1.1] - 2026-04-05
### Added
- Web UI session memory
- ReAct history compaction
- HTTP 400 tool-call fallback

### Fixed
- Credential path sandbox
- Reliable tool confidence seeding
- Stale build cleanup

## [1.1.0] - 2026-04-01
### Added
- Context keyword search
- Confidence seeding with prompt surfacing
- DRY full-pipeline CostTracker

## [1.0.9] - 2026-03-25
### Added
- Dynamic `tools.yaml` wiring
- Confidence feedback loop
- Multi-agent synthesis
- Per-task cost aggregation

## [1.0.8] - 2026-03-20
### Fixed
- Approval flow: asyncio import, resolve card on respond
- North star conflict waits for user, emits `task_cancelled` on conflict reject
- Clean CLI exit after approval

## [1.0.7] - 2026-03-15
### Added
- Inference strategy (eco / cruise / sport) with UI indicator
- User-defined cron schedules and `schedule_task` tool
- One-shot jobs
- Inline approval widget
- Markdown chat rendering

## [1.0.6] - 2026-03-10
### Added
- Inline approval widget
- Persistent user cron schedules
- Dynamic job queue UI

## [1.0.5] - 2026-03-05
### Added
- Persistent memory via ledger
- Real web search (DuckDuckGo)
- readline word-jump shortcuts
- 404 model fallback

### Changed
- ReAct format cleanup

## [1.0.4] - 2026-03-01
### Added
- All agents converted to agentic ReAct loop
- Code agent and general agent
- Coding tools (bash, patch_file, git)
- Workspace support

## [1.0.3] - 2026-02-25
### Added
- CLI chat interface
- General agent
- Web chat thread

### Fixed
- Platform compatibility fixes

## [1.0.2] - 2026-02-20
### Added
- Docker Compose containerisation
- Drag-and-drop file upload
- Cross-platform port management

## [1.0.1] - 2026-02-15
### Changed
- Updated UI/UX
- Router mechanism updated to use multiple channels

## [1.0.0] - 2026-02-10
### Added
- Initial stable release
