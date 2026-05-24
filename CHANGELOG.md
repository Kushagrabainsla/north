# Changelog

All notable changes to north are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Inference strategy (eco / cruise / sport)** — `config/strategy.py` introduces `StrategyMode` and `NorthSettings` (persisted to `~/.north/settings.json`). The `OpenRouterInferenceRouter` builds a model fallback chain at call time based on the active strategy: `eco` tries cheapest first, `sport` tries most capable first, `cruise` (default) maps `PoolPriority` to an appropriate starting tier then falls through adjacent tiers. Free models are always appended as the last resort. Strategy is changed via natural language ("switch to eco mode") or `POST /orchestrator/settings` and takes effect immediately with no restart.

- **Strategy indicator in UI** — terminal prompt shows the active mode in colour (`[eco] ❯` green, `[cruise] ❯` cyan, `[sport] ❯` yellow); Web UI command bar shows a matching badge that refreshes after each task completes.

- **Inline approval widget in Web UI** — when an agent emits `approval_required` mid-loop, an approval card appears inline inside the thinking bubble with the agent's message and action buttons. Clicking a button POSTs to `/orchestrator/approval/respond` and replaces the widget with a confirmation.

- **Markdown rendering in chat** — north's responses in the Web UI are now rendered as formatted markdown (via `marked.js`) rather than plain text. Code blocks, headers, lists, and inline code all display correctly.

- **Persistent conversation history** — `north chat` loads the last 20 task/response pairs from the ledger at startup and injects them as conversation context so the agent remembers prior sessions.

- **User-defined cron schedules** — `jobs/cron_store.py` adds `UserCronStore`, backed by a `user_cron_entries` table in `~/.north/jobs.db`. The `CronScheduler` now merges built-in and user entries on each iteration (max 60 s delay for new entries to take effect). Managed via `POST/GET/DELETE /orchestrator/cron` or the Web UI Jobs page.

- **One-shot scheduled jobs** — agents can schedule a task for a specific future datetime via the `schedule_task` tool (`run_at` param). Users can also create them from the Jobs page "+ Schedule" form or `POST /orchestrator/jobs`.

- **`schedule_task` tool** — available to all domain agents (health, university, job, finance, general). Accepts `run_at` (one-shot) or `hour`/`minute`/`weekday` (recurring). Recurring entries are persisted via `UserCronStore`; one-shot tasks are enqueued directly into `jobs.db`.

- **Jobs page redesign** — `/ui/jobs` now has two sections: "Recurring Schedules" (user cron entries with delete, "+ Add" form) and "One-Shot & Queue" (job table with "+ Schedule" form and status filters).

### Changed
- **`NorthStarChecker` uses `PoolPriority.MEDIUM`** (was `HIGH`) — the alignment check is a structured JSON classification task, not deep reasoning; fast/cheap models handle it reliably and the change avoids burning the reasoning pool on every consequential task.

- **North star failure is non-fatal** — if inference is unavailable during the north star check, the orchestrator logs a warning, emits `north_star_aligned` (skip), and the task continues. Only an actual goal conflict stops execution.

- **`CronScheduler.run()` no longer exits on empty entries** — previously returned immediately when `entries` was empty. Now loops with a 60 s sleep so user-added entries are picked up without a restart.

- **File drag-and-drop** on two surfaces:
  - *Context page drop zone* (`context_index.html`): drop a PDF, DOCX, TXT, or MD file onto the drop zone to inject it directly into the context store via `POST /orchestrator/context/add`. Visual feedback (border highlight, status message) on drag-over and after upload.
  - *Command bar* (`dashboard.html`): drop a file onto the command bar while composing a task. The file is uploaded first via `POST /orchestrator/context/add`, then the task is submitted with the file name appended to the prompt. A dismissable file chip shows the attached filename.
  - Drag-over state tracked with a depth counter (prevents false `dragleave` on child element entry). Click-to-browse also supported on the context drop zone.
  - CSS additions in `web/static/css/main.css`: `.drop-zone`, `.drop-zone.drag-active`, `.command-bar.drag-active`, `.file-chip`, `.drop-zone__status`.

- **Docker / container support:**
  - `Dockerfile` — `python:3.12-slim` base; installs `uv`; installs north via `uv pip install --system -e .`; copies source; sets `NORTH_HOME=/data`; exposes 8000.
  - `docker-compose.yml` — single `north` service; mounts `north_data` volume at `/data`; passes `NORTH_HOME`, `NORTH_SECRET`, `NORTH_OPENROUTER_API_KEY`, `NORTH_ENV` from the host environment; `restart: unless-stopped`; healthcheck polls `/docs`.
  - `.dockerignore` — excludes `.venv/`, `__pycache__/`, test artifacts, `.env*`, and `.git/`.

- **`north stop` CLI command** — `docker compose down` when Docker is available; otherwise kills the process listening on port 8000 via psutil.

- **`NORTH_HOME` env var** in `config/settings.py` — `north_home` field now reads `os.environ.get("NORTH_HOME", "~/.north")` so the Docker volume mount (`/data`) takes effect without the doubled `NORTH_` pydantic-settings prefix.

- **`NORTH_SECRET` env var** in `config/settings.py` — `north_secret` field reads `os.environ.get("NORTH_SECRET", "")`. The `secret` property prefers the env var over the `secret.key` file, so Docker deployments can pass the secret without a volume-mounted key file.

- **`psutil>=5.9.0`** added to `[project.dependencies]` — cross-platform process/port management for `north start` conflict detection and `north stop`.

### Changed
- **`north start` uses Docker Compose by default** — if a `docker-compose.yml` is found in the CWD or any parent directory and Docker is available, `north start` calls `docker compose up --build`. The `--local` flag forces the old uvicorn path regardless. Falls back to uvicorn with a warning when Docker is unavailable.

- **Cross-platform port conflict handling in `north start`** — replaced macOS-only `lsof -ti :8000 | xargs kill -9` with psutil: iterates `psutil.net_connections()`, finds the PID listening on the configured port, and kills it. No subprocess or platform-specific command.

- **`TerminalNotifier` replaces `MacOSNotifier` as the default notifier** — `config/dependencies.py` now wires `TerminalNotifier()` in `build_production_dependencies()`. `MacOSNotifier` remains in `approval/macos.py` and can be re-wired manually on macOS. Removes the macOS-only constraint from the production dependency graph.

- **`POST /context/add` accepts `Form` parameters** — removed the Pydantic `ContextAddRequest` body model; `text` and `url` are now `Form(None)` parameters. Fixes a FastAPI constraint where the presence of `UploadFile` forces multipart mode and makes a JSON body unparseable.

- **`POST /task` accepts both form-encoded and JSON bodies** — the endpoint now reads `content-type` and parses `application/x-www-form-urlencoded` or `multipart/form-data` via `request.form()`, falling back to `request.json()`. Fixes HTMX form submissions which send `application/x-www-form-urlencoded` by default.

- **`README.md` Section 9.1** — updated to note that `TerminalNotifier` (stdout/logs) is the default; `alerter` is optional and macOS-only.
- **`README.md` Section 14** — added `Dockerfile`, `docker-compose.yml`, `.dockerignore` to the repository structure.
- **`README.md` Section 16.10** — "macOS Notifications" renamed to "Notifications"; documents `TerminalNotifier` as default and `alerter` as optional upgrade on macOS.
- **`README.md` Section 16.11** — corrected env var names: `NORTH_HOME` and `NORTH_SECRET` (not doubled `NORTH_NORTH_*`); added `NORTH_SECRET` entry.
- **`README.md` Section 16.12** — restructured Getting Started into Path A (Docker, recommended for server/headless) and Path B (local macOS install); added `north stop` command; `--local` flag documented.
- **`README.md` Section 16.13** — added `psutil>=5.9.0` to the complete dependency list.

### Added
- `CHANGELOG.md`, following the Keep a Changelog convention. Every change to north now appends an entry here (see `docs/CODING_STYLE.md` Section 23.5).
- `pyproject.toml` with project metadata and `pytest` + `pytest-asyncio` configuration matching the test conventions in `docs/CODING_STYLE.md` Section 18.
- `tests/` scaffolding: `tests/conftest.py`, `tests/unit/`, `tests/integration/`, and a smoke test (`tests/unit/test_smoke.py`) that passes from a fresh clone.
- `docs/CODING_STYLE.md` Section 23 "Working with Claude Code", codifying five collaboration rules: ask when unsure (23.1), confirm before substantive changes with a similar-pattern batching exception (23.2), tech decisions follow research → reason → propose → apply (23.3), tests are co-authored with code (23.4), every change updates this changelog (23.5).
- `docs/CODING_STYLE.md` Section 18.8, a cross-reference pointing the testing rules forward to Section 23.4.
- `README.md` Section 15 new entry "Offline Transcription Fallback" — names the reliability gap introduced by Decision 3 (cloud-only STT) without committing to a fix.
- `CONTRIBUTING.md` at the repo root — covers the three flows required by `docs/CODING_STYLE.md` Section 21.3 (adding agents, adding tools, running tests) and restates the process rules from Section 23.
- `CODE_OF_CONDUCT.md` at the repo root — Contributor Covenant v2.1 reference with a project-specific enforcement note.
- `SECURITY.md` at the repo root — vulnerability reporting channel and the in/out-of-scope list (X-North-Secret bypass, callback forgery, prompt-injection exfiltration, ledger tampering).
- `.env.example` at the repo root — required + optional env vars under the `NORTH_` prefix, documenting the canonical names that `pydantic-settings` will read.
- `docs/CODING_STYLE.md` Section 23.6 "New Standards Land in This File" — when the user states a rule of practice, capture it tersely in the right section before acting; the file must stay lean.
- `docs/CODING_STYLE.md` tightened across Sections 2 (SOLID), 3 (Design Patterns — now a table), 4 (Clean Code Rules), 5 (DRY), 6 (Plug and Play), and 23 (Working with Claude Code). File went 2054 → 1562 lines (~24% reduction). Redundant "wrong vs correct" example pairs collapsed to single rules; multi-paragraph prose tightened to single paragraphs. All reference content preserved: interface map (6.1), full Dependencies dataclass + builder pair (6.3), TOOL_GRAPH (6.5), LedgerSource/LedgerStatus enums (5.5), module layout (7.3), Settings class (17.1), .gitignore (20.4), .env.example (21.2).
- `README.md` Section 14 `ledger/` entry brought into line with the more detailed `docs/CODING_STYLE.md` Section 7.3 layout. Old entry listed `ledger.py` and `schema.py`; new entry reflects what actually shipped: `__init__.py`, `base.py`, `models.py`, `exceptions.py`, `sqlite_writer.py`.
- `pyproject.toml` test config now sets `pythonpath = ["."]` so pytest can import the project's top-level modules (`ledger`, `utils`, `exceptions`, `context`) without a package install. Required because the project deliberately ships modules at the repo root rather than under a `src/` or `north/` package directory.
- `README.md` Section 14 `context/` entry expanded to match the actual layout: `__init__.py`, `base.py` (ABC), `models.py` (ContextDocument enum), `exceptions.py`, `file_store.py` (concrete). Previously listed a single `store.py` that combined the ABC and the concrete; the split matches `ledger/`'s pattern and the rest of `docs/CODING_STYLE.md` Section 6.1.
- `docs/CODING_STYLE.md` Section 6.1 interface map updated: `context/store.py` → `context/base.py` to match the new layout.

### Added
- First runnable module: **`ledger/`**, the append-only audit trail (README Section 4).
  - `ledger/models.py` — `LedgerEntry` (Pydantic model), `LedgerSource` enum (9 values matching README Section 4.3), `LedgerStatus` enum (7 values matching the schema in 4.2).
  - `ledger/exceptions.py` — `LedgerError`, `LedgerWriteError`, `LedgerReadError`, all inheriting from `exceptions.NorthError`.
  - `ledger/base.py` — `LedgerWriter` ABC with `write`, `get`, `query`; `LedgerFilters` dataclass for query parameters.
  - `ledger/sqlite_writer.py` — `SQLiteLedgerWriter`, the concrete implementation. Schema initialized on construction; `asyncio.to_thread` wraps blocking I/O so callers stay non-blocking on the event loop (docs/CODING_STYLE.md Section 14.1); `sqlite3.Error` is caught at the boundary and re-raised as a north exception (Section 13.4).
- `exceptions.py` at the repo root — `NorthError` base class. All module-specific exceptions inherit from it.
- `utils/` module: `utils/db.py:open_db_connection()` is the single SQLite connection helper used by every `*.db` file under `~/.north/` (docs/CODING_STYLE.md Section 11.1).
- Tests under `tests/unit/ledger/`: 14 unit tests covering enum coverage against the spec, Pydantic field validation, write→get round-trip across all 15 ledger columns, every supported `LedgerFilters` field (task_id, agent, source, since), descending-timestamp ordering, limit honoring, and duplicate-id rejection as `LedgerWriteError`. Tests added in the same change as the module (docs/CODING_STYLE.md Section 23.4).
- `pydantic>=2.0.0` added to `[project.dependencies]`. Already approved in README Section 16.13 — installed now that real code needs it.
- Second runnable module: **`context/`**, the five-document context layer (README Section 5).
  - `context/models.py` — `ContextDocument` enum with the five valid document names (`public.md`, `private.md`, `privacy_rules.md`, `judgement_rules.md`, `north_stars.md`).
  - `context/exceptions.py` — `ContextError`, `ContextReadError`, `ContextWriteError`, all inheriting from `exceptions.NorthError`.
  - `context/base.py` — `ContextStore` ABC with async `read`, `write`, `append`; default `search()` raises `NotImplementedError` (the v1 seam for a future `DBContextStore`).
  - `context/file_store.py` — `FileContextStore`, the v1 concrete. Reads return `""` for documents that have never been written. `append()` separates entries with a single `\n` and creates the document if missing. Base directory is created on construction.
- Tests under `tests/unit/context/`: 11 unit tests covering enum coverage against the spec, missing-document reads, write/read round-trip, write overwrites, append separator behavior (between entries and on first write), independent storage per document, `search()` raising in v1, base-directory creation, and constructor idempotency over an existing directory with prior content.
- Test packages: `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/ledger/__init__.py`, `tests/unit/context/__init__.py`. Required so pytest can collect tests with the same basename (`test_models.py`) under different subdirectories.
- Third runnable module: **`jobs/`**, the persistent job queue and cron scheduler (README Section 11).
  - `jobs/models.py` — `Job` (Pydantic), `JobType` enum (cron/event/async/retry), `JobStatus` enum (pending/running/completed/failed/cancelled), `JobPriority` IntEnum (HIGH=1, MEDIUM=2, LOW=3).
  - `jobs/exceptions.py` — `JobError`, `JobNotFoundError`, `JobProcessingError`, all inheriting from `exceptions.NorthError`.
  - `jobs/base.py` — `JobProcessor` ABC with `enqueue`, `get`, `claim_next`, `mark_completed`, `mark_failed` (with optional `retry_after`), `cancel`, `list_jobs`.
  - `jobs/sqlite_processor.py` — `SQLiteJobProcessor`. `claim_next` runs inside `BEGIN IMMEDIATE` so two concurrent claimers can never pick the same job. Status transitions are explicit; `cancel` is a no-op if the job is already terminal.
  - `jobs/scheduler.py` — asyncio-native cron implementation per Decision 4 (CHANGELOG above). `CronEntry` dataclass validates hour/minute/weekday on construction. `next_firing(entry, after)` and `next_due_entry(entries, after)` are pure functions, fully testable without time mocking. `CronScheduler.run()` is the loop: `next_due_entry → asyncio.sleep → processor.enqueue`.
- `utils/ids.py` — `generate_id()` (32-char hex from UUID4) and `generate_task_id()` (`task_` prefix + 12 hex chars). Used by `jobs/scheduler.py` to mint Job IDs; will be used by `ledger/` and `orchestrator/` as those modules grow.
- Tests under `tests/unit/jobs/` and `tests/unit/utils/`: 36 new tests covering enum/spec coverage, queue CRUD round-trip, atomic claim ordering by `(priority ASC, scheduled_at ASC)`, future-scheduled and retry_after skip behavior, completed/failed/cancelled state transitions, mark_failed-with-retry leaving the job pending and incrementing retry_count, cancel-is-no-op-on-terminal, list filtering and limit, CronEntry input validation, next_firing math across all daily and weekly cases (including the "strictly after" edge), next_due_entry earliest-pick, and the empty-entries short-circuit on `CronScheduler.run()`.
- Test packages: `tests/unit/jobs/__init__.py`, `tests/unit/utils/__init__.py`.
- Fourth runnable module: **`tools/`**, the tool layer (README Section 7).
  - `tools/models.py` — `ToolInput` (params envelope), `ToolOutput` (`success: bool`, `data`, optional `error`), `ConfidenceScore` (one persisted (agent, tool) row).
  - `tools/exceptions.py` — `ToolError`, `ToolNotFoundError`, `ToolExecutionError`, `ToolAuthError`.
  - `tools/base.py` — `Tool` ABC (`name`, `description`, `async run`), `AuthenticatedTool` (adds `validate_credentials`), `CacheableTool` (adds `get_cached` / `set_cached`).
  - `tools/confidence.py` — `ConfidenceTracker` over `tools.db`. Math per README 7.5: `DEFAULT_CONFIDENCE=0.5`, `CONFIDENCE_INCREASE=0.05`, `CONFIDENCE_DECREASE=0.03`, clamped to `[0.0, 1.0]`. `record_use` updates `confidence`, `uses_total`, `uses_helpful`, `last_updated` atomically. `scores_for_agent` returns `(tool_name, score)` pairs ordered by confidence descending. `inherit_from(new_agent, source_agent)` copies rows for the `similar_to` feature via `INSERT OR IGNORE` — idempotent and preserves existing rows.
  - `tools/registry.py` — `TOOL_GRAPH` constant (canonical agent → tools mapping for the four v1 agents) and `ToolRegistry` class (`register`, `get`, `tools_for_agent`, `agent_names`, `all_tool_names`). Accepts a custom graph for tests.
- Tests under `tests/unit/tools/`: 29 new tests covering ToolInput/ToolOutput/ConfidenceScore validation, ABC instantiation failure when abstract methods are missing (Tool, AuthenticatedTool, CacheableTool), confidence math (helpful increase, unhelpful decrease, cap at 1.0 over 50 calls, floor at 0.0 over 50 calls), counter increments, ordering of `scores_for_agent` (DESC), empty return for unseen agents, `inherit_from` copying rows and preserving existing target rows, persistence across `ConfidenceTracker` instances, `TOOL_GRAPH` covering the four v1 agents with cross-domain tools, `ToolRegistry.register`/`get` round-trip and `ToolNotFoundError` on miss, `tools_for_agent` filtering to only registered names, empty return for unknown agents, `agent_names`/`all_tool_names` over the graph, and custom-graph injection.
- Test package: `tests/unit/tools/__init__.py`.
- `docs/CODING_STYLE.md` Section 23.4 rewritten: "Tests Are Currently Deferred." New code lands without tests during the pre-MVP build-out phase. The pytest harness, Section 18 conventions, and the existing 92 tests stay in place for when the policy is lifted.
- Fifth runnable module: **`inference/`**, the Inference Router (README Section 8).
  - `inference/models.py` — `PoolPriority` enum (HIGH/MEDIUM/LOW, mapped to reasoning/fast_cheap/high_volume), `ModelPool`, `CompletionRequest`/`CompletionResponse`, `TranscriptionRequest`/`TranscriptionResponse`, `InferenceRecord`, `CostSummary`. `PRIORITY_TO_POOL` maps priorities to pool names so the router's selection is data-driven.
  - `inference/exceptions.py` — `InferenceError`, `AllModelsRateLimitedError`, `PoolRefreshError`, `TranscriptionError`.
  - `inference/base.py` — `InferenceRouter` ABC with `complete`, `transcribe`, `get_model`, `refresh_pools`, `current_pools`. Both completion and transcription land here per Decision 3 (single provider, single API key).
  - `inference/fallback_pools.py` — hardcoded minimal pools used at startup when no `inference_cache.json` exists. Three pools, three models each. Plus `DEFAULT_TRANSCRIPTION_MODEL = "groq/whisper-large-v3"` (Decision 3).
  - `inference/openrouter.py` — `OpenRouterInferenceRouter` concrete. Constructor loads pools in preference order: `inference_cache.json` → `FALLBACK_POOLS`. `refresh_pools()` fetches `/api/v1/models`, buckets by completion price (top third → reasoning, middle → fast_cheap, bottom → high_volume), and writes the snapshot back to the cache. `complete()` walks the priority's pool, retrying on `429` until exhausted (then raises `AllModelsRateLimitedError`). `transcribe()` posts base64-encoded audio to `/api/v1/audio/transcriptions` using the default Groq Whisper Large v3 (overridable per call). `aclose()` for clean shutdown via the FastAPI lifespan.
  - `inference/__init__.py` — public exports.
- `pyproject.toml` adds `httpx>=0.27.0` to `[project.dependencies]`. Already approved in README Section 16.13; installed now that `inference/` needs it.
- No new tests this change. Per the new `docs/CODING_STYLE.md` Section 23.4 policy, code lands without tests during the pre-MVP phase.

### Changed
- `.gitignore` expanded from a single `.envrc` entry to the full layout specified in `docs/CODING_STYLE.md` Section 20.4 (Python, tool caches, env files, OS, editor).
- `README.md` Section 16.10: macOS notification dependency switched from `terminal-notifier` to `alerter` (Swift fork by vjeantet). `terminal-notifier` removed action button support and its own maintainer points users to `alerter` for that use case; the rest of north's Approval Layer flow (Section 9) is unchanged because the call shape is identical.
- `README.md` Sections 10.1, 14, 16.8, 16.13: Web UI stack switched from React 18 + Vite + TypeScript + Tailwind to HTMX + Jinja2 (server-rendered, no npm, no build step). UI mounts at `localhost:8000/ui` instead of `localhost:3000`; SSE uses HTMX's SSE extension; approval cards are `<form hx-post>` elements; auth secret moves from in-memory React state to an HttpOnly session cookie. `web/src/` and `web/public/` directories become `web/templates/` and `web/static/`.
- `README.md` Sections 3.1, 8.6 (new), 16.6, 16.13: voice transcription switched from local `faster-whisper` to OpenRouter's audio transcription endpoint (`POST /api/v1/audio/transcriptions`, announced May 2026). Reuses the existing `OPENROUTER_API_KEY` and the existing `httpx` client — no new dependency. Default transcription model is `groq/whisper-large-v3` (sub-second latency); selectable alternatives include `openai/whisper-1`, `openai/gpt-4o-transcribe`, `google/chirp-3`. Section 3.1's local-only stance is explicitly reversed; the Inference Router (Section 8) now owns transcription on the same fallback and cost-logging path as LLM calls. Capture hotkey changed from `Fn` (collides with macOS system Dictation) to a configurable default of `Right Option + Space`.
- `README.md` Sections 11.3, 16.13: cron scheduling switched from `apscheduler` to a single in-house asyncio background task. `jobs/scheduler.py` holds `(hour, minute, weekday)` tuples and computes the next firing across all entries. Resolves the prior contradiction between Section 16.4 ("asyncio only, no extra concurrency frameworks") and the apscheduler dependency. `apscheduler` removed from the dependency list; no replacement library added.
- `pyproject.toml` and `README.md` Section 16.11: migrated dev dependencies from the deprecated `[tool.uv].dev-dependencies` table to the standard `[dependency-groups].dev` table (PEP 735). uv was emitting a deprecation warning on every command with the old form.
- `README.md` Section 15 "Mobile App" item annotated to note that the HTMX switch in Section 16.8 dropped the implementation cost from "separate codebase" to "template-level changes." Deferral stands.
- Moved `CODING_STYLE.md` from the repo root to `docs/CODING_STYLE.md`. Keeps GitHub-auto-detected community files at root (README, LICENSE, CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, .env.example), and gives future architecture/design docs a natural home. All references in README.md and CHANGELOG.md updated; test-file docstring references intentionally left for a later cleanup.
- `README.md` env var naming brought into line with the `Settings` class declared in `docs/CODING_STYLE.md` Section 17.1 (which uses `env_prefix = "NORTH_"`). Four prose references and the env-block in Section 16.11 updated: `OPENROUTER_API_KEY` → `NORTH_OPENROUTER_API_KEY`, `NORTH_HOME` → `NORTH_NORTH_HOME`, `NORTH_ENV` → `NORTH_NORTH_ENV`. The doubled `NORTH_` prefix is intentional: it is what pydantic-settings actually reads from the environment given the current field names.
- `README.md` Section 16.12 restructured into two paths: "For users" (the intended `curl -LsSf https://north.dev/install.sh | sh` flow that bootstraps `uv`, Python 3.12+, `alerter`, the `north` package via `uv tool install`, secret generation, API-key prompt, and an opt-in LaunchAgent for auto-start) and "For developers" (the existing `git clone` + `uv sync` flow). Installer script itself is not yet implemented; PyPI package name `north` and install host `north.dev` are placeholders pending availability checks.
