# Changelog

All notable changes to north are documented here.

## [1.3.6] - 2026-06-09
### Added
- **`shell` tool — long-lived PTY sessions** (`tools/specialized/shell_tool.py`): start/read/write/stop/list actions keep a process alive across tool calls behind a pseudo-terminal (stdlib `pty`, no new dependency) for dev servers, `--watch` builds, REPLs, and debuggers. `start` and `write` are approval-gated like `bash`; output streams into a per-session ring buffer. Wired into coder + tester.
- **Diff preview before write** (`tools/specialized/patch_file.py`): when an `ApprovalStore` is injected, `patch_file` computes the change, shows a unified diff in an approval card, and writes only on confirm — rejection leaves the file untouched. Falls back to immediate apply when no store is present.
- **`gh` tool** (`tools/specialized/gh_tool.py`): GitHub CLI wrapper for the PR/issue workflow — `pr_view`, `pr_diff`, `pr_checks`, `pr_comment`, `pr_review`, `issue_view`, etc. Read-only actions run immediately; mutating ones follow the GitTool pattern (model calls `request_approval` first). Auth delegated to `gh`. Wired into the coder agent's `tools.yaml`.
- **Repository convention auto-discovery** (`context/repo_instructions.py`): when a task carries a `workspace`, agents auto-load `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and `.cursorrules` from the workspace root and enclosing git root, injected as a "Repository conventions" context section via `Agent._load_context`.
- **`glob` tool** (`tools/universal/glob.py`): name-based file lookup by glob pattern (`**/*Test*.ts`), results sorted newest-first, noise dirs (`.git`, `node_modules`, …) pruned. Universal — every agent gets it.
- **`read_file` line ranges** (`tools/universal/read_file.py`): `start_line` / `end_line` params read a slice of a large file instead of the whole thing; output is now line-numbered (`cat -n` style). Closes the drift where this signature was documented but unimplemented.
- **`search_files` output modes** (`tools/universal/search_files.py`): `output_mode` (`content` / `files_with_matches` / `count`), `context` lines around each hit, `file_type` language shorthand (`py`, `ts`, `go`, …) and `head_limit` — grep parity without an external ripgrep binary. Now also searches a single-file path, not just directories.
- **`patch_file` ordered edits** (`tools/specialized/patch_file.py`): `edits` param applies a list of `{old_string, new_string}` replacements in one call, each required to be unique when applied; the file is written only if every edit succeeds (atomic).

### Changed
- **Shared tool approval flow** (`tools/specialized/_approval.py`): `BashTool`, `ShellTool`, and `PatchFileTool` now route their JudgementFilter→card→wait approval through one helper (`request_approval_decision`) instead of three copies (DRY, §5).
- **Serialized mutating tool calls** (`agents/agentic_llm_agent.py`): tools declare `is_mutating` (`tools/base.py`); the ReAct loop runs read-only calls concurrently but mutating ones (file writes, shell, git, gh, delegate) sequentially, so two edits to the same file in one turn can no longer race and lose an update.
- **Shared subprocess runner** (`tools/specialized/_subprocess.py`): `GitTool` and `GhTool` share one `run_capture()` (timeout, output cap, structured result) instead of duplicated `_run_sync` bodies (DRY, §5).
- **`ApprovalDecision.TIMEOUT_REJECTED`** (`approval/models.py`): approval decisions are now an enum throughout instead of bare `"rejected"`/`"timeout_rejected"` string literals (§5.5).

### Fixed
- **Tool-loop crash on exception** (`agents/agentic_llm_agent.py`): the parallel tool-call `gather` ran without `return_exceptions=True` (violating §10.5); a raise in `_request_approval` could crash the whole agent run and cancel sibling calls. Each call is now wrapped so a failure becomes a failed tool result.
- **Persistent-shell output loss** (`tools/specialized/shell_tool.py`): `_reap_exited` drained an exited session's buffer via `read_new()`, silently destroying unread output when `list`/`start` ran. It now uses a non-destructive `has_pending_output()` check.
- **`ApprovalStore` unbounded growth** (`approval/store.py`): resolved cards accumulated forever; the registry now evicts the oldest resolved cards past a cap (pending cards are never evicted). Docstring corrected from "thread-safe" to "coroutine-safe".
- **`search_files` duplicate context lines** (`tools/universal/search_files.py`): overlapping `context` windows around adjacent matches no longer emit the same line multiple times.
- **Tool-result truncation overflow** (`agents/agentic_llm_agent.py`): large non-string fields bypassed the per-field cap; a bounded valid-JSON fallback now guarantees the result stays under the limit.
- **Misleading terminal message** (`agents/agentic_llm_agent.py`): an empty `tool_calls` response no longer reports "reached the maximum number of reasoning steps".
- **Ledger insert column drift** (`ledger/sqlite_writer.py`): INSERT placeholders are derived from the column tuple instead of a hand-synced `"?" * 17` (§9.6).

## [1.3.5] - 2026-06-08
### Added
- **Three-layer BashTool command safety** (`tools/specialized/bash.py`):
  - Layer 1 — `CommandSafetyInspector`: instant prefix-match bypass for read-only commands (`git status`, `cat`, `ls`, `grep`, `find`, `pwd`, `whoami`) — zero LLM cost, zero latency
  - Layer 2 — `JudgementFilter` integration: auto-approve/reject based on learned user rules from `judgement_rules.md`, reducing repeat approval prompts over time
  - Layer 3 — Manual approval card: unchanged fallback for unknown or mutating commands
  - Each layer short-circuits: if Layer 1 approves, Layers 2–3 never run
- **SEARCH/REPLACE diff blocks** (`tools/specialized/patch_file.py`): `PatchFileTool` now accepts `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` blocks in `new_string`, enabling multi-hunk edits in a single tool call
- **Multi-language symbol search** (`tools/semantic/search_symbols.py`): `SearchSymbolsTool` now finds classes and functions in TypeScript/JavaScript (`.ts`, `.js`, `.tsx`, `.jsx`) and Go (`.go`) files via regex, in addition to existing Python AST parsing
- **Structured compiler diagnostics** (`tools/analysis/check_types.py`): `_parse_error_line()` extracts `{file, line, column, severity, message}` dicts from mypy, tsc, and `go vet` output — agents get structured data instead of raw text
- **Semantic code tools** (`tools/semantic/`): Four new AST-aware and grep-based tools for agents:
  - `read_file(path, start_line?, end_line?)` — read file ranges with line numbers (faster than bash)
  - `list_dir(path)` — explore directory structure without spawning shell
  - `search_symbols(path, type?)` — find function/class definitions via Python AST (Python files only)
  - `find_references(symbol, path)` — locate all uses of a symbol via regex search
  - Agents use these instead of bash for faster, more reliable code exploration
- **Type awareness tool** (`tools/analysis/check_types.py`): Run language-specific type checkers immediately after code changes:
  - Python: `mypy --no-error-summary`
  - TypeScript: `npx tsc --noEmit`
  - Go: `go vet ./...`
  - Coder agent calls this after every file edit; prevents type errors from propagating
- **Task context snapshots** (`context/task_snapshot.py`): Persistent, versioned task state for cross-agent continuity:
  - Stored at `.north/tasks/{task_id}/context_snapshot.json`
  - Tracks: task ID, original request, branch name, stage, prior agent visits, failure count, test status
  - Agents load snapshot at startup; enables loop detection and smart escalation without re-reading artifacts
  - `TaskContextSnapshot` dataclass + `TaskContextSnapshotStore` for read/write
- **Enhanced delegation protocol** (`agents/schemas.py`): `delegate_task` schema extended with optional context field:
  - Agents pass metadata (failed_attempts, known_failures, relevant_files) when delegating
  - Receiving agent unpacks context and avoids redundant work
  - Enables tester to avoid re-running failed tests; architect to escalate early on loops
- **Coder agent system prompt** updated (`agents/coder/prompts/system.md`):
  - Added workflow step 1: load task context snapshot at startup
  - Step 5 now instructs use of `read_file`, `search_symbols`, `find_references` before modifying code
  - Step 5 calls `check_types` after every file edit instead of manual compile checks
  - Step 8 (handoff to tester) now passes context with failure_count for loop detection
- **Tester agent system prompt** updated (`agents/tester/prompts/system.md`):
  - Added workflow step 1: load task context snapshot to understand prior attempts
  - Step 3 loop detection now references snapshot failure_count; escalates to architect if >= 3

### Changed
- `JudgementFilter` instantiated once at startup and shared between `Orchestrator` and `BashTool` — eliminates duplicate construction
- `_build_tool_registry()` and `_build_orchestrator()` accept `judgement_filter` as a parameter instead of creating their own
- `git diff` output formatting improved for cleaner agent context
- Coder workflow simplified: `check_types` replaces language-specific bash compile checks; fewer shell commands

### Docs
- `docs/TECHNICAL_FEATURES.md` §13: new section documents the three-layer BashTool command safety architecture
- `docs/CODING_STYLE.md` §23.7: new pre-commit checklist rule — every commit must update changelog, version (`pyproject.toml` + `uv lock`), docs, and commit message before it is complete
- `docs/CODING_STYLE.md` §16 (Tools): new section describes semantic tools (read_file, list_dir, search_symbols, find_references, check_types) and when to use them instead of bash.
- `docs/ARCHITECTURE.md`: updated §8.1 (multi-provider table replacing "all inference through OpenRouter"), §8.2 (continuous `quality_from_cost()` scoring replacing old thirds-bucketing description), §8.5 (correct exception class names `ModelRateLimitedError`/`PaymentRequiredError`/`InferenceError` and accurate fallback semantics), §7.5 (added `consecutive_failures` column to schema), §10.2 (removed stale `north chat`, corrected `context show` command, added `north metrics`, added TUI invocation), §16.3 (seven databases, not six — added `tool_index.db` and `facts.db`), §16.11 (added `NORTH_GROQ_API_KEY`, `NORTH_GEMINI_API_KEY`, and tuning env vars).
- `docs/TECHNICAL_FEATURES.md`: §2 replaced stale `bucket_models()` pseudocode with actual `quality_from_cost()` + threshold-bin pattern; §4 corrected pool refresh description (startup explicit call + sleep-first background loop).
- `README.md`: added optional provider keys section, updated usage to include TUI invocation, `north metrics`, `north stream`, and corrected `north stop` flag.

### Fixed
- `AgenticLLMAgent._execute()` now catches `ContextTooLargeError` raised by `complete_with_tools`, compacts the history to `keep_recent=1`, and retries once before returning a graceful error message.
- **`InferenceError` fallback** (`inference/dispatcher.py`): provider-level errors (e.g. HTTP 400 — unsupported parameters) now advance the fallback chain to the next candidate instead of re-raising immediately. The EMA failure is recorded and a `WARNING` is logged before continuing; only unexpected non-inference exceptions still re-raise.

## [1.3.3] - 2026-06-06
### Added
- **Multi-provider inference** (`inference/dispatcher.py`): `ModelDispatcher` implements `InferenceRouter` across an ordered list of providers; routes each call to the best available model regardless of which provider hosts it
- **GroqRouter** (`inference/groq.py`): free-tier completions and tool calls via `llama-3.3-70b-versatile`, `mixtral-8x7b-32768`, `llama-3.1-8b-instant`; Whisper transcription via multipart upload
- **GeminiRouter** (`inference/gemini.py`): free-tier completions via `gemini-2.0-flash`, `gemini-1.5-pro`, `gemini-1.5-flash`; embeddings via `text-embedding-004`
- **`OpenAICompatibleProvider`** (`inference/openai_compat.py`): shared HTTP base class for all OpenAI-format providers; adding a new provider requires only a subclass with `name`, `base_url`, and optional `embed`/`transcribe` overrides
- **`ModelCapability` / `ModelInfo`** (`inference/capability.py`): typed capability flags (`COMPLETION`, `TOOL_CALLS`, `EMBEDDING`, `TRANSCRIPTION`) and an immutable per-model descriptor with `context_window`, `cost_per_token`, and `base_quality`
- **`Provider` protocol** (`inference/provider.py`): runtime-checkable interface all routers satisfy; the dispatcher only talks to this — never to concrete router classes
- **Per-model cooldowns**: dispatcher tracks `(model_id, provider_name) → skip_until` timestamp; rate-limited models cool for 60 s, payment-required models cool for 24 h
- **Context window filter + `ContextTooLargeError`**: dispatcher estimates input tokens (`chars / 4`), filters models that are too small, and raises `ContextTooLargeError` when no candidate fits — signals the agent layer to compact and retry
- **Priority ranking**: `HIGH` → quality descending (best model first); `MEDIUM` → free models first then quality; `LOW` → cost ascending then quality
- **`NORTH_GROQ_API_KEY` / `NORTH_GEMINI_API_KEY`** settings: adding either key to `~/.north/.env` activates that provider with no code changes

### Changed
- `OpenRouterInferenceRouter` renamed to `OpenRouterRouter`; now extends `OpenAICompatibleProvider` instead of `InferenceRouter` directly; pool-walking retry logic removed (dispatcher owns all routing)
- `factory.py` builds a `ModelDispatcher` from available provider keys; OpenRouter is always the last provider in the chain as the broadest fallback
- `inference/__init__.py` updated to export all new public types

### Fixed
- `402 Payment Required` from OpenRouter no longer crashes north; the dispatcher applies a 24 h cooldown to the (model, provider) pair and falls through to the next candidate automatically

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
