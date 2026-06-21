# north: Coding Style Guide
> For contributors and Claude Code. Read this in full before writing any code.

---

## Table of Contents

1. [Guiding Principles](#1-guiding-principles)
2. [SOLID Principles](#2-solid-principles)
3. [Design Patterns in north](#3-design-patterns-in-north)
4. [Clean Code Rules](#4-clean-code-rules)
5. [DRY: Don't Repeat Yourself](#5-dry-dont-repeat-yourself)
6. [Plug and Play: Interfaces First](#6-plug-and-play-interfaces-first)
7. [Modularity](#7-modularity)
8. [Project Structure](#8-project-structure)
9. [Python Style](#9-python-style)
10. [Async](#10-async)
11. [SQLite](#11-sqlite)
12. [FastAPI](#12-fastapi)
13. [Error Handling](#13-error-handling)
14. [Ledger Writes](#14-ledger-writes)
15. [Agents](#15-agents)
16. [Tools](#16-tools)
17. [Configuration](#17-configuration)
18. [Testing](#18-testing)
19. [Documentation](#19-documentation)
20. [Git](#20-git)
21. [Open Source Standards](#21-open-source-standards)
22. [What Not to Build](#22-what-not-to-build)
23. [Working with Claude Code](#23-working-with-claude-code)

---

## 1. Guiding Principles

These principles govern every line of code in north. When in doubt, come back here.

**Clean Code (Robert C. Martin)**
Code is read far more than it is written. Every function, class, and module must be immediately understandable without explanation. Names explain intent. Functions do one thing. Classes have one reason to change.

**SOLID**
The five object-oriented design principles that make code maintainable, extensible, and testable. Covered fully in Section 2.

**DRY: Don't Repeat Yourself**
Every piece of knowledge has a single, authoritative representation in the system. If you find yourself writing the same logic in two places, stop. Extract it. The duplication is telling you something is missing.

**Plug and Play**
Every layer of north can be swapped without touching other layers. The Orchestrator does not care which agents exist. Agents do not care which tools exist. Tools do not care which LLM is running. Interfaces enforce this boundary. Concrete implementations are wired once at startup.

**Modularity**
Each module owns one concept. It exposes a clean public interface and hides its implementation. You can understand, test, and replace a module without reading any other module.

**Open Source First**
north is public. Every contributor should be able to read the codebase, understand it, and contribute without needing to ask questions. Clarity is more important than cleverness.

---

## 2. SOLID Principles

Every class and interface in north satisfies all five. The north-specific application of each:

### 2.1 SRP - Single Responsibility

One reason to change per class. If you describe a class with "and," split it. north's `InferenceRouter` selects models and refreshes pools; cost tracking lives in a separate `CostTracker`.

### 2.2 OCP - Open/Closed

Add behavior by adding a class, not by modifying an existing one. New tools subclass `Tool` and register in the tool graph. No existing file changes.

```python
class LinkedInSearchTool(Tool):
    name = "linkedin_search"
    async def run(self, input: ToolInput) -> ToolOutput: ...
```

### 2.3 LSP - Liskov Substitution

Any concrete implementation must be fully substitutable for its ABC. Code written against `ContextStore` must work identically with `FileContextStore` or a future `DBContextStore`. Subclasses must not raise on methods the parent supports, narrow argument types, or widen return types.

### 2.4 ISP - Interface Segregation

Clients depend only on what they use. north splits the tool hierarchy:

```python
class Tool(ABC): ...                              # base: just run()
class AuthenticatedTool(Tool, ABC): ...           # adds validate_credentials()
class CacheableTool(Tool, ABC): ...               # adds get_cached/set_cached()
```

Each tool implements only the ABCs it needs. `WebSearchTool(Tool)`; `GmailTool(AuthenticatedTool)`; `MarketDataTool(AuthenticatedTool, CacheableTool)`.

### 2.5 DIP - Dependency Inversion

High-level modules depend on ABCs, not concretes. Dependencies are injected at the boundary, never instantiated inside.

```python
class Orchestrator:
    def __init__(self, ledger: LedgerWriter) -> None:
        self.ledger = ledger

orchestrator = Orchestrator(ledger=SQLiteLedgerWriter(db_path))  # wired once, at startup
```

The Orchestrator never knows whether the ledger is SQLite, Postgres, or an in-memory mock.

---

## 3. Design Patterns in north

Use these where they fit. Do not invent new structural patterns without a strong reason.

| Pattern | Where it lives in north |
|---------|-------------------------|
| **Strategy** | Every swappable interface: `ContextStore`, `LedgerWriter`, `InferenceRouter`, `Notifier`, `Tool`. Concrete chosen at startup; callers never see which one. |
| **Registry** | `AgentRegistry` and `ToolRegistry` discover members at runtime (filesystem walk, tool graph). Adding a member requires no registry code change. |
| **Template Method** | The `Agent` ABC fixes the `run()` skeleton (load context → load tools → `_execute()` → format result). Subclasses override `_execute()` only. |
| **Repository** | `LedgerWriter`, `ContextStore`, `JobProcessor` expose domain methods; the storage engine (SQLite, files) is hidden behind them. |
| **Factory** | `AgentFactory` builds agents from a folder path. `CardFactory` picks the right card type from an `AgentResult`'s classification flags. |
| **Observer** | The Ledger is a passive observer: components fire-and-forget via `spawn(ledger.write(...), name=...)` (`utils/tasks.py`); the extraction pipeline reads new entries on its own loop. No pub/sub framework. |

---

## 4. Clean Code Rules

### 4.1 Functions Do One Thing

If you can meaningfully extract another function, the current one is doing too much. The Orchestrator's `process_task()` reads like a sentence: `classify_intent → check_north_star_alignment → build_execution_plan → execute_plan`. No step's logic lives inside the parent.

### 4.2 Names Explain Intent

Never use `handle`, `process`, `manage`, `do`, or `run` alone. Name the specific action: `classify_intent`, `run_agent_for_task`, `extract_context_deltas_from_ledger`.

### 4.3 Functions Are Short

If a function doesn't fit on one screen (~20–30 lines), split it.

### 4.4 Argument Count: Maximum Three

More than three arguments means the function is doing too much or related fields belong in a model. Wrong: `write_ledger_entry(source, task_id, agent, input, ...)`. Right: `write(self, entry: LedgerEntry)`.

### 4.5 No Comments That Explain What

If a comment explains *what* the code does, rename things until the code explains itself. Comments are reserved for *why* - a non-obvious constraint, a workaround, a surprising fact.

```python
# acceptable: explains why, not what
# OpenRouter returns Retry-After in seconds, not milliseconds
cooldown = int(error_headers.get("Retry-After", DEFAULT_COOLDOWN_SECONDS))
```

### 4.6 Classes Have One Reason to Change

Two reasons to change = two classes. See Section 2.1.

### 4.7 No Dead Code

Never leave commented-out code. Delete it. Git history preserves everything.

### 4.8 Avoid Negative Conditionals

Prefer positive predicates or extract to a named boolean. `if classification.is_consequential` beats `if not classification.is_not_trivial`.

### 4.9 Early Return Over Nesting

Return early on failure; do not nest the happy path. Reject invalid input, reject conflicts, reject anything that can't proceed - *then* run the success path at the bottom unindented.

---

## 5. DRY: Don't Repeat Yourself

### 5.1 One Definition, Many Uses

Every piece of logic exists in exactly one place. SQLite connection setup with WAL pragmas lives in `utils/db.py:open_db_connection()`; `ledger/` and `jobs/` both import it. If you find yourself copy-pasting, extract.

### 5.2 Shared Utilities Live in utils/

Logic used by more than one module lives in `utils/`; never re-implemented inline.

```
utils/
  db.py          <- open_db_connection()
  ids.py         <- generate_id(), generate_task_id()
  time.py        <- utcnow(), format_timestamp()
  security.py    <- generate_secret(), load_secret(), verify_secret()
  prompts.py     <- load_prompt(path: str | Path) -> str
  tasks.py       <- spawn(coro, *, name)  # supervised fire-and-forget background task
```

### 5.3 Prompts Are Files, Not Strings

Every LLM prompt is a markdown file under `prompts/`, loaded via `utils.prompts.load_prompt()`. No prompt string is hardcoded in Python. Shared base prompts are loaded and extended; never duplicated.

### 5.4 Models Are Defined Once

Every Pydantic model is defined once in its module's `models.py`. Other modules import the canonical type - they never redefine a shape that already exists. `from ledger import LedgerEntry`, not `class TaskLedgerEntry(BaseModel): ...` somewhere else.

### 5.5 Enums Replace All Repeated Strings

Any string that appears in more than one file is an enum, defined once and imported. The canonical example is `LedgerSource` (Section 4.3 of the README) and `LedgerStatus`, both in `ledger/models.py`:

```python
class LedgerSource(str, Enum):
    PROMPT = "prompt"
    MIC = "mic"
    CRON = "cron"
    AGENT = "agent"
    ASYNC = "async"
    SYSTEM = "system"
    MANUAL_INJECTION = "manual_injection"
    INFERENCE_ROUTER = "inference_router"
    APPROVAL = "approval"
    WEBHOOK = "webhook"
    CLARIFICATION = "clarification"

class LedgerStatus(str, Enum):
    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    AWAITING_INPUT = "awaiting_input"
```

---

## 6. Plug and Play: Interfaces First

### 6.1 The Full Interface Map

These abstract base classes must exist. Every concrete implementation fulfills exactly one of these. No other component depends on a concrete class directly.

```
context/base.py        ContextStore      <- FileContextStore (v1), DBContextStore (future)
ledger/base.py         LedgerWriter      <- SQLiteLedgerWriter
inference/base.py      InferenceRouter   <- ModelDispatcher (multi-provider)
approval/base.py       Notifier          <- TerminalNotifier (default), MacOSNotifier (optional);
                                            wrapped at runtime by TUIAwareNotifier
tools/base.py          Tool              <- WebSearchTool, ReadFileTool, BashTool, GitTool, etc.
agents/base.py         Agent             <- LLMAgent / AgenticLLMAgent (subclassed per domain:
                                            ArchitectAgent, CoderAgent, TesterAgent, ResearcherAgent,
                                            HealthAgent, JobAgent, FinanceAgent, UniversityAgent, …)
jobs/base.py           JobProcessor      <- SQLiteJobProcessor
```

### 6.2 Interfaces Are Small and Focused

Each ABC declares only the methods every implementation must support. Optional behavior splits into additional ABCs (ISP, Section 2.4). The `search()` placeholder on `ContextStore` is intentional - it raises in v1's `FileContextStore` and gets a real implementation in `DBContextStore` later. See README Section 5.2 for the canonical definition.

### 6.3 Dependency Injection at the Boundary

Concrete implementations are wired once in `config/dependencies.py`. No module instantiates its own dependencies - everything goes in via constructors.

```python
# config/dependencies.py
@dataclass
class Dependencies:
    context_store: ContextStore
    ledger: LedgerWriter
    inference_router: InferenceRouter
    notifier: Notifier
    job_processor: JobProcessor

def build_production_dependencies() -> Dependencies:
    return Dependencies(
        context_store=FileContextStore(settings.north_home / "context"),
        ledger=SQLiteLedgerWriter(settings.north_home / "ledger.db"),
        inference_router=build_router(openrouter_api_key=settings.openrouter_api_key),
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(settings.north_home / "jobs.db"),
    )

# Test wiring lives in tests/conftest.py as build_test_dependencies(tmp_path).
# It builds the same Dependencies with temp-dir stores and a MockInferenceRouter.
```

Swapping any backend (e.g. `FileContextStore` → `DBContextStore`) is one line.

### 6.4 Agents Are Discovered, Not Registered

The Orchestrator scans `/agents` at startup. A valid agent folder contains `config.yaml` and `agent.py`. Adding an agent = adding a folder. The registry exposes `get(name)`, `all()`, and `for_domain(domain)`. No registry code changes when agents come or go.

### 6.5 Tools Are Discovered from the Filesystem

`ToolRegistry` (`tools/registry.py`) auto-discovers every `Tool` subclass by walking the
tool package directories - it does **not** read a hand-maintained graph dict. Adding a tool
is adding a file:

```
tools/
  universal/    <- available to EVERY agent (read_file, write_file, glob, list_dir,
                   search_files, web_search, fetch_url, schedule_task, create_tool,
                   create_agent, query_metrics)
  specialized/  <- opt-in per agent (bash, shell, git, gh, patch_file, kasa)
  semantic/     <- code intelligence (search_symbols, find_references)
  analysis/     <- static analysis (check_types)
```

The agent→tool mapping is a **dynamic graph**, not a constant. Universal tools are granted
to all agents; each agent additionally lists its specialized tools in its own `tools.yaml`:

```yaml
# agents/coder/tools.yaml
tools:
  - bash
  - shell
  - git
  - gh
  - patch_file
```

The registry exposes `tools_for_agent(agent)` (universal + that agent's specialized set,
sorted by confidence) and `update_graph(agent, tool_names)` for runtime changes (e.g. a tool
hot-loaded mid-task by `create_tool`). `make_universal(name)` promotes a tool to the
all-agents set. No registry code changes when tools come or go.

---

## 7. Modularity

### 7.1 Module Boundaries Are Hard

A module never reaches into another module's internals. It calls only the public interface declared in `__init__.py`.

```python
# wrong: reaching into ledger internals
from ledger.sqlite_writer import _build_insert_sql
from ledger.schema import SOURCE_MAPPING

# correct: public interface only
from ledger import LedgerWriter, LedgerEntry, LedgerSource
```

### 7.2 Every Module Has an Explicit Public Interface

Every module directory has an `__init__.py` that declares exactly what is public. Anything not listed in `__all__` is private.

```python
# ledger/__init__.py
from ledger.base import LedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from ledger.exceptions import LedgerWriteError, LedgerReadError

__all__ = [
    "LedgerWriter",
    "LedgerEntry",
    "LedgerSource",
    "LedgerStatus",
    "LedgerWriteError",
    "LedgerReadError",
]
```

### 7.3 Standard Module Layout

Every module follows the same internal structure. This makes every module immediately predictable.

```
{module}/
  __init__.py        <- public interface: exports and __all__
  base.py            <- abstract interface (ABC) for swappable modules
  models.py          <- Pydantic models and enums owned by this module
  exceptions.py      <- custom exception classes for this module
  {implementation}.py   <- concrete implementation(s)
```

Full layout for every module in north:

```
ledger/
  __init__.py
  base.py            <- LedgerWriter (ABC)
  models.py          <- LedgerEntry, LedgerSource, LedgerStatus
  exceptions.py      <- LedgerWriteError, LedgerReadError
  sqlite_writer.py   <- SQLiteLedgerWriter

context/
  __init__.py
  base.py            <- ContextStore (ABC)
  models.py          <- ContextDocument (enum of valid document names)
  exceptions.py      <- ContextReadError, ContextWriteError
  file_store.py      <- FileContextStore
  fact_store.py      <- atomic fact store
  episodic.py        <- episodic memory layer
  embedding_index.py <- semantic search / cosine-similarity index
  extraction.py      <- ledger → context extraction pipeline
  injection.py       <- manual context injection
  repo_instructions.py <- loads AGENTS.md / CLAUDE.md / .cursorrules for a workspace
  task_snapshot.py   <- per-task context snapshot

inference/
  __init__.py
  base.py            <- InferenceRouter (ABC)
  models.py          <- ModelPool, PoolPriority, InferenceRecord, CostSummary
  exceptions.py      <- AllModelsRateLimitedError, ContextTooLargeError, PoolRefreshError
  constants.py       <- quality tiers and tuning constants
  capability.py      <- ModelCapability, ModelInfo, quality_from_cost
  provider.py        <- Provider (Protocol)
  dispatcher.py      <- ModelDispatcher (multi-provider router)
  routing.py         <- strategy-aware model ordering
  cooldowns.py       <- per-model rate-limit cooldown tracking
  factory.py         <- build_router()
  cost_tracker.py    <- CostTracker (InferenceRouter decorator)
  providers/
    openai_compat.py <- OpenAICompatibleProvider (base class for OpenAI-format providers)
    openrouter.py    <- OpenRouter provider
    groq.py          <- Groq provider
    gemini.py        <- Gemini provider

approval/
  __init__.py
  base.py             <- Notifier (ABC)
  models.py           <- Card, CardType, ApprovalDecision
  exceptions.py       <- NotificationError
  macos.py            <- MacOSNotifier (optional native macOS alerts)
  terminal.py         <- TerminalNotifier (default)
  tui.py              <- TUIAwareNotifier (wraps a Notifier; silent while the TUI is attached)
  interaction.py      <- UserInteraction (single path for approval/question/information cards)
  callback_server.py  <- FastAPI app on port 8001
  store.py            <- module-level approval_store singleton (card registry)
  judgement_filter.py <- pre-screens cards against judgement_rules.md

agents/
  __init__.py
  base.py              <- Agent (ABC)
  models.py            <- AgentPayload, AgentResult, AgentConfig
  exceptions.py        <- AgentNotFoundError, AgentExecutionError
  constants.py         <- agent tuning constants (iteration caps, tool-result limits)
  schemas.py           <- DELEGATE_TASK_SCHEMA, REQUEST_APPROVAL_SCHEMA
  registry.py          <- AgentRegistry (folder discovery)
  llm_agent.py         <- LLMAgent (single-call base)
  agentic_llm_agent.py <- AgenticLLMAgent (ReAct loop, native function calling)
  context_compaction.py <- token-aware history compaction
  workspace_lock.py    <- per-workspace mutation lock
  coder/               <- one folder per domain agent: agent.py + config.yaml +
    agent.py           <- CoderAgent              tools.yaml + prompts/
    config.yaml
    tools.yaml
    prompts/
  architect/  tester/  researcher/  general/  home/  news_briefing/
  health/  job/  finance/  university/

tools/
  __init__.py
  base.py            <- Tool, AuthenticatedTool, CacheableTool
  models.py          <- ToolInput, ToolOutput, ConfidenceScore
  exceptions.py      <- ToolExecutionError, ToolAuthError
  registry.py        <- ToolRegistry (filesystem discovery + dynamic agent→tool graph)
  tool_index.py      <- tool metadata index
  confidence.py      <- ConfidenceTracker (EMA scoring)
  _path.py           <- shared path-safety helpers
  universal/         <- granted to every agent (read_file, write_file, glob, list_dir,
                        search_files, web_search, fetch_url, schedule_task,
                        create_tool, create_agent, query_metrics)
  specialized/       <- opt-in per agent (bash, shell, git, gh, patch_file, kasa)
  semantic/          <- code intelligence (search_symbols, find_references)
  analysis/          <- static analysis (check_types)

orchestrator/
  __init__.py
  models.py          <- TaskRequest, TaskResponse, ExecutionPlan, IntentClassification
  exceptions.py      <- OrchestratorError, NorthStarConflictError, RoutingError
  constants.py       <- orchestrator tuning constants
  orchestrator.py    <- Orchestrator
  router.py          <- ExecutionPlanner (intent classification + execution planning)
  north_star.py      <- NorthStarChecker
  synthesizer.py     <- multi-agent result synthesis
  task_context.py    <- TaskContextStore
  failure_handler.py <- FailureHandler
  stream.py          <- EventStreamManager (SSE event stream)
  app.py             <- FastAPI app + lifespan (background tasks)
  api_router.py      <- REST routes (secret-gated APIRouter)

jobs/
  __init__.py
  base.py            <- JobProcessor (ABC)
  models.py          <- Job, JobStatus, JobType
  exceptions.py      <- JobNotFoundError, JobProcessingError
  sqlite_processor.py <- SQLiteJobProcessor
  scheduler.py       <- CronScheduler

utils/
  db.py              <- open_db_connection()
  ids.py             <- generate_id(), generate_task_id()
  time.py            <- utcnow(), localnow(), format_timestamp()
  security.py        <- generate_secret(), load_secret(), verify_secret()
  prompts.py         <- load_prompt()
  net.py             <- SSRF-safe HTTP helpers
  text.py            <- shared text helpers
  math.py            <- shared numeric helpers (EMA, clamps)
  logging.py         <- logging setup
  version.py         <- NORTH_VERSION

config/
  settings.py        <- Settings (BaseSettings)
  dependencies.py    <- Dependencies, build_production_dependencies()

cli/
  main.py            <- Typer app, all commands in one file
  tui.py             <- Textual TUI client
  _client.py         <- HTTP client for the local server
  _server.py         <- server lifecycle helpers

tests/
  unit/
  integration/
  conftest.py        <- shared pytest fixtures
```

---

## 8. Project Structure

Follow the layout in Section 7.3 exactly. Do not create new top-level directories without a spec update.

---

## 9. Python Style

### 9.1 Formatting and Linting

- Formatter: `ruff format`
- Linter: `ruff check`
- Line length: 100 characters
- Always run both before committing

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "C4", "PIE", "SIM"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]   # allow assert in tests
```

### 9.2 Type Hints on Everything

All function signatures have type hints. Return types are always declared. Use `from __future__ import annotations` at the top of every file for forward references.

```python
from __future__ import annotations

# correct
async def classify_intent(prompt: str) -> IntentClassification: ...
def get_agent(name: str) -> Agent | None: ...
async def write(self, entry: LedgerEntry) -> str: ...

# wrong
async def classify_intent(prompt): ...
def get_agent(name): ...
```

### 9.3 Naming Conventions

| Kind | Convention | Example |
|------|-----------|---------|
| Functions and methods | `snake_case` | `classify_intent` |
| Variables | `snake_case` | `task_id` |
| Classes | `PascalCase` | `LedgerWriter` |
| Abstract interfaces | `PascalCase`, no prefix | `ContextStore` not `IContextStore` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_TIMEOUT_SECONDS` |
| Private methods/attributes | `_leading_underscore` | `_load_agents` |
| Files | `snake_case.py` | `sqlite_writer.py` |
| Enums | `PascalCase` class, `UPPER_SNAKE_CASE` members | `LedgerSource.AGENT` |

### 9.4 Import Order

```python
from __future__ import annotations

# 1. standard library
import asyncio
import importlib
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

# 2. third-party
import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# 3. local: always from the module's public __init__.py
from agents import Agent, AgentPayload, AgentResult
from config.settings import settings
from ledger import LedgerEntry, LedgerSource, LedgerStatus, LedgerWriter
from utils.ids import generate_task_id
```

No wildcard imports. Ever.

### 9.5 String Formatting

f-strings only. Not `.format()`, not `%`.

```python
# correct
message = f"Agent {agent.name} completed task {task_id} in {elapsed:.2f}s"

# wrong
message = "Agent {} completed task {} in {}s".format(agent.name, task_id, elapsed)
```

### 9.6 No Magic Values

Every meaningful string or number is a named constant or enum. Defined once, imported everywhere.

```python
# config/settings.py or the relevant module's models.py
DEFAULT_AGENT_READ_TIMEOUT_SECONDS: int = 30
CONFIDENCE_AUTO_APPROVE_THRESHOLD: float = 0.8
TASK_CLEANUP_COMPLETED_DAYS: int = 7

# wrong: magic values inline
await asyncio.wait_for(coro, timeout=30)
if rule.confidence >= 0.8:
conn.execute("DELETE FROM tasks WHERE age > 7")
```

### 9.7 Dataclasses for Value Objects, Pydantic for I/O

Use `@dataclass` for internal value objects with no validation. Use `BaseModel` for anything that crosses a boundary (API, database, file).

```python
# internal value object: dataclass
@dataclass
class ExecutionPlan:
    task_id: str
    agents: list[str]
    parallel_groups: list[list[str]]
    dependencies: dict[str, list[str]]

# API boundary: Pydantic
class TaskRequest(BaseModel):
    prompt: str
    source: LedgerSource = LedgerSource.PROMPT

class TaskResponse(BaseModel):
    task_id: str
    status: str
```

---

## 10. Async

### 10.1 All I/O is Async

Every database call, HTTP call, file read, and file write uses `async/await`. Never use synchronous I/O in an async context.

```python
# correct
async def read_context_document(document: str) -> str:
    return await asyncio.to_thread(_sync_read, document)

# wrong: blocks the event loop
def read_context_document(document: str) -> str:
    with open(document) as f:
        return f.read()
```

### 10.2 Parallel Agents

```python
agent_results = await asyncio.gather(
    *[agent.run(payload) for agent in agents_in_group],
    return_exceptions=True,
)

# handle results including partial failures
for agent, result in zip(agents_in_group, agent_results):
    if isinstance(result, Exception):
        await handle_agent_failure(agent, result, task_id)
    else:
        await task_context.write(task_id, agent.name, result)
```

### 10.3 Never Block the Event Loop

Wrap synchronous blocking calls with `asyncio.to_thread()`.

```python
result = await asyncio.to_thread(sync_blocking_call, arg1, arg2)
```

### 10.4 Background Tasks in Lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    tasks = [
        asyncio.create_task(job_processor.run(), name="job_processor"),
        asyncio.create_task(extraction_pipeline.run(), name="extraction_pipeline"),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

### 10.5 Always Handle asyncio.gather Exceptions

`asyncio.gather` with `return_exceptions=True` prevents one agent failure from cancelling all other agents. Always use `return_exceptions=True` when running multiple independent coroutines.

---

## 11. SQLite

### 11.1 One Connection Helper, Used Everywhere

```python
# utils/db.py: defined once
def open_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn
```

### 11.2 Raw SQL, Parameterized Always

No ORM. Raw SQL. Always use `?` placeholders.

```python
# correct
conn.execute(
    "SELECT * FROM ledger WHERE agent = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
    (agent_name, LedgerStatus.COMPLETED, limit),
)

# wrong: SQL injection risk
conn.execute(f"SELECT * FROM ledger WHERE agent = '{agent_name}'")
```

### 11.3 Context Managers for Transactions

```python
with open_db_connection(db_path) as conn:
    conn.execute("INSERT INTO ledger VALUES (?, ?, ?, ?)", values)
    # auto-commits on exit, auto-rollbacks on exception
```

### 11.4 Each Store Owns Its Schema

There is no central migrations module. Each SQLite-backed store creates and evolves its own
schema at construction time using idempotent `CREATE TABLE IF NOT EXISTS` (and additive
`ALTER TABLE` guarded by a column check). This keeps each store self-contained - the store
that owns a table owns its schema.

```python
# ledger/sqlite_writer.py - schema set up once, on construction
def _ensure_schema(self) -> None:
    with open_db_connection(self._db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger ("
            "  id TEXT PRIMARY KEY, source TEXT, task_id TEXT, ...)"
        )
```

Stores that follow this pattern: `ledger/sqlite_writer.py`, `jobs/sqlite_processor.py`,
`jobs/cron_store.py`, `context/{fact_store,episodic,embedding_index}.py`.
`open_db_connection()` in `utils/db.py` is the single connection helper (WAL pragmas, row
factory) used by all of them.

If a future change needs cross-store versioned migrations, introduce a real `utils/migrations.py`
with a spec update first (§8) - do not invent one ad hoc.

---

## 12. FastAPI

### 12.1 Pydantic Models on Every Endpoint

```python
class TaskRequest(BaseModel):
    prompt: str
    source: LedgerSource = LedgerSource.PROMPT

class TaskResponse(BaseModel):
    task_id: str
    status: str
    created_at: str

@app.post("/orchestrator/task", response_model=TaskResponse, status_code=202)
async def submit_task(request: TaskRequest) -> TaskResponse:
    return await orchestrator.handle_task(request)
```

### 12.2 Routes Are One Line

Route functions call one function and return its result. Business logic lives in the module.

```python
# correct: one line
@app.post("/orchestrator/task", response_model=TaskResponse)
async def submit_task(request: TaskRequest) -> TaskResponse:
    return await orchestrator.handle_task(request)

@app.get("/orchestrator/tasks", response_model=list[TaskResponse])
async def list_tasks() -> list[TaskResponse]:
    return await orchestrator.list_active_tasks()

# wrong: business logic in the route
@app.post("/orchestrator/task")
async def submit_task(request: TaskRequest):
    task_id = generate_task_id()
    classification = await classify(request.prompt)
    # 40 more lines ...
```

### 12.3 Auth Dependency on Every Route

```python
# utils/security.py: defined once
async def verify_request_secret(x_north_secret: str = Header(...)) -> None:
    if not verify_secret(x_north_secret):
        raise HTTPException(status_code=403, detail="Invalid secret.")

# applied to every route via a router-level dependency
api_router = APIRouter(dependencies=[Depends(verify_request_secret)])

@api_router.post("/orchestrator/task", response_model=TaskResponse)
async def submit_task(request: TaskRequest) -> TaskResponse:
    ...
```

### 12.4 Routes Are Grouped by Module

Each module has its own `router.py`. The main `app.py` assembles them.

```python
# orchestrator/router.py
from fastapi import APIRouter
router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])

# agents/router.py
from fastapi import APIRouter
router = APIRouter(prefix="/agents", tags=["agents"])

# orchestrator/app.py
from fastapi import FastAPI
from orchestrator.router import router as orchestrator_router
from agents.router import router as agents_router

app = FastAPI()
app.include_router(orchestrator_router)
app.include_router(agents_router)
```

### 12.5 Explicit HTTP Status Codes

```python
# correct
raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")
raise HTTPException(status_code=403, detail="Invalid secret.")
raise HTTPException(status_code=400, detail=f"Agent '{name}' is not registered.")
raise HTTPException(status_code=409, detail=f"Task {task_id} is already running.")

# wrong
raise HTTPException(status_code=500, detail="Something went wrong.")
raise HTTPException(status_code=400, detail="Bad request.")
```

---

## 13. Error Handling

### 13.1 Custom Exception Hierarchy

Every module defines its own exceptions in `exceptions.py`. All exceptions inherit from a `NorthError` base class so callers can catch north-specific errors when needed.

```python
# exceptions.py (root)
class NorthError(Exception):
    """Base class for all north exceptions."""
    pass

# ledger/exceptions.py
from exceptions import NorthError

class LedgerError(NorthError): pass
class LedgerWriteError(LedgerError): pass
class LedgerReadError(LedgerError): pass

# agents/exceptions.py
from exceptions import NorthError

class AgentError(NorthError): pass
class AgentNotFoundError(AgentError): pass
class AgentExecutionError(AgentError): pass
class AgentTimeoutError(AgentError): pass

# inference/exceptions.py
from exceptions import NorthError

class InferenceError(NorthError): pass
class AllModelsRateLimitedError(InferenceError): pass
class PoolRefreshError(InferenceError): pass

# tools/exceptions.py
from exceptions import NorthError

class ToolError(NorthError): pass
class ToolExecutionError(ToolError): pass
class ToolAuthError(ToolError): pass
class ToolNotFoundError(ToolError): pass
```

### 13.2 Catch Specific, Re-raise or Handle

```python
# correct: catch the specific exception you expect
try:
    response = await client.get(OPENROUTER_MODELS_URL, timeout=10)
    response.raise_for_status()
except httpx.TimeoutException as e:
    raise PoolRefreshError("OpenRouter model refresh timed out.") from e
except httpx.HTTPStatusError as e:
    raise PoolRefreshError(f"OpenRouter returned {e.response.status_code}.") from e

# wrong: swallowing or catching too broadly
try:
    response = await client.get(url)
except Exception:
    pass
```

### 13.3 Errors Write to the Ledger

Every caught error that affects system behavior writes a Ledger entry. Never swallow errors silently.

```python
try:
    result = await agent.run(payload)
except AgentExecutionError as e:
    spawn(
        self._ledger.write(LedgerEntry(
            source=LedgerSource.SYSTEM,
            task_id=payload.task_id,
            agent=agent.name,
            action="agent_execution_failed",
            output=str(e),
            status=LedgerStatus.FAILED,
        )),
        name="error_ledger",
    )
    await self._failure_handler.handle(payload.task_id, agent.name, e)
    raise
```

### 13.4 Wrap Third-Party Exceptions at the Boundary

Do not let third-party exceptions (httpx, sqlite3) leak into application code. Catch them at the module boundary and re-raise as a north exception.

```python
# inference/openrouter.py: catch httpx at the boundary
try:
    response = await self._client.post(url, json=payload)
except httpx.RequestError as e:
    raise InferenceError(f"Request to OpenRouter failed: {e}") from e

# orchestrator.py: only sees InferenceError, never httpx.RequestError
try:
    model = await self._inference_router.get_model(priority)
except InferenceError as e:
    await self._handle_inference_failure(task_id, e)
```

---

## 14. Ledger Writes

### 14.1 Always Fire and Forget

Ledger writes are side effects. They stay off the main execution path, so a slow or failing write never blocks the task.

```python
# correct: supervised fire-and-forget (see utils/tasks.py)
spawn(
    self._ledger.write(LedgerEntry(
        source=LedgerSource.AGENT,
        task_id=task_id,
        agent=self.name,
        output=result.summary,
        agent_output=result.data,
        status=LedgerStatus.COMPLETED,
    )),
    name="agent_completion_ledger",
)

# wrong: blocking the main path
await self._ledger.write(LedgerEntry(...))
```

### 14.2 Always Use Enums

```python
# correct
from ledger import LedgerSource, LedgerStatus

source=LedgerSource.AGENT
status=LedgerStatus.COMPLETED

# wrong
source="agent"
status="completed"
```

---

## 15. Agents

### 15.1 The Agent Interface

```python
# agents/base.py
from abc import ABC, abstractmethod
from agents.models import AgentPayload, AgentResult

class Agent(ABC):
    """Abstract base class for all north agents.

    Every agent implements this interface. The Orchestrator calls run() and
    does not know or care which concrete agent it is calling.
    """

    name: str       # must be set as a class variable
    domain: str     # must be set as a class variable

    async def run(self, payload: AgentPayload) -> AgentResult:
        """Template method. Do not override. Implement _execute instead."""
        context = await self._load_context(payload)
        tools = await self._load_tools()
        raw = await self._execute(payload, context, tools)
        return self._format_result(raw)

    @abstractmethod
    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        tools: list[Tool],
    ) -> dict:
        """Domain-specific execution logic. Implement this in each agent."""
        ...

    async def _load_context(self, payload: AgentPayload) -> str:
        """Default: read public.md and judgement_rules.md. Override if needed."""
        ...

    async def _load_tools(self) -> list[Tool]:
        """Default: load tools by confidence score from the tool graph. Override if needed."""
        ...

    def _format_result(self, raw: dict) -> AgentResult:
        """Default: wrap raw dict in AgentResult. Override if needed."""
        return AgentResult(**raw)
```

### 15.2 Agents Never Import From Each Other

```python
# wrong
from agents.finance.agent import FinanceAgent
budget = await FinanceAgent().get_budget(task_id)

# correct: read through the Task Context Object
from orchestrator.task_context import TaskContextStore

budget = await self._task_context.read(
    task_id=payload.task_id,
    requesting_agent=self.name,
    key="finance.budget",
    required=True,
)
```

### 15.3 Agent Output Is Always AgentResult

```python
# agents/models.py
class AgentResult(BaseModel):
    output: str           # human-readable result
    summary: str          # one-line summary for the Ledger
    data: dict            # full structured output for Task Context Object
    requires_approval: bool = False
    has_question: bool = False
    question: str | None = None
    question_options: list[str] = []
```

---

## 16. Tools

### 16.1 The Tool Interface

```python
# tools/base.py
from abc import ABC, abstractmethod
from tools.models import ToolInput, ToolOutput

class Tool(ABC):
    name: str
    description: str

    @abstractmethod
    async def run(self, input: ToolInput) -> ToolOutput: ...

class AuthenticatedTool(Tool, ABC):
    @abstractmethod
    async def validate_credentials(self) -> bool: ...

class CacheableTool(Tool, ABC):
    @abstractmethod
    async def get_cached(self, key: str) -> ToolOutput | None: ...

    @abstractmethod
    async def set_cached(self, key: str, result: ToolOutput) -> None: ...
```

### 16.1.1 Filesystem Write Policy

Every path-touching tool resolves through `tools/_path.py:resolve_path()` (the single fail-closed gate). Two - and only two - write zones exist:

1. **The workspace** - all user-facing work (code, edited files, deliverables) lands here, in place. This is the deliverable; review it via `git diff`.
2. **`<NORTH_HOME>/tasks/{task_id}/`** - internal agent handoff scratch only (research notes, specs, QA reports). A narrow carve-out inside the otherwise-blocked `~/.north`, available regardless of the active workspace.

Everything else under `~/.north` stays blocked - secrets (`secret.key`), `.env`, and all `*.db` (including the task-context DBs that share `~/.north/tasks/`). Agents never hardcode the handoff path: the orchestrator injects the absolute dir via `_path.handoff_dir_for()` into the `## Handoff Directory` prompt section, so it is defined once and never drifts.

### 16.1.2 Output Verification

Don't trust an agent's narrative - verify it against evidence. An LLM writes what
a successful answer *sounds like* ("created the file", "tests pass"), with no idea
whether it is true. `orchestrator/verification.py:verify_claims()` cross-checks
action claims in the final answer against the tools that actually **succeeded**
(`AgentResult.successful_tools`). Unsupported claims are non-fatally flagged: the
output is annotated and a `claims_unverified` ledger entry is recorded, so a
fabricated completion is visible rather than silently marked clean. Patterns
favour precision (flag, don't cry wolf); `successful_tools is None` (a non-tool
agent) skips the check.

### 16.2 Confidence Scoring

```python
# tools/confidence.py
class ConfidenceTracker:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def get_score(self, agent: str, tool: str) -> float:
        ...

    async def record_use(self, agent: str, tool: str, was_helpful: bool) -> None:
        delta = CONFIDENCE_INCREASE if was_helpful else CONFIDENCE_DECREASE
        new_score = await self._apply_delta(agent, tool, delta)
        spawn(self._log_to_ledger(agent, tool, new_score), name="confidence_ledger")

    async def get_tools_for_agent(self, agent_name: str) -> list[tuple[Tool, float]]:
        """Return tools for agent sorted by confidence score descending."""
        ...
```

### 16.3 Semantic Code Tools

Agents explore code via semantic tools instead of spawning shell commands. These are faster and more reliable.

| Tool | Purpose | Example |
|------|---------|---------|
| `read_file(path, start_line?, end_line?)` | Read file ranges with line numbers | `read_file("agents/base.py", 1, 50)` |
| `glob(pattern, path?, head_limit?)` | Find files by name, newest first | `glob("**/*Test*.ts")` |
| `list_dir(path)` | Explore directory structure | `list_dir("tools/")` |
| `search_files(pattern, output_mode?, context?, file_type?)` | Grep contents: lines, file list, or counts | `search_files("def run", output_mode="count")` |
| `search_symbols(path, type?)` | Find function/class definitions via Python AST | `search_symbols("tools/base.py", "class")` |
| `find_references(symbol, path)` | Locate all uses of a symbol | `find_references("execute_call", "agents/")` |
| `check_types(path)` | Run language-specific type checkers | `check_types("agents/base.py")` |

**When to use instead of bash:**
- `read_file` instead of `cat` / `head` / `tail`
- `glob` instead of `find -name`
- `list_dir` instead of `ls` / `find`
- `search_files` instead of `grep -rn` (use `output_mode` to match `-l` / `-c`)
- `search_symbols` instead of `grep "^def \|^class "` (Python)
- `find_references` instead of `grep -r` (when looking for symbol usage)
- `check_types` instead of `python -m py_compile` / `tsc --noEmit` / `go vet`

**When to still use bash:**
- `bash` is appropriate for one-shot commands that run and exit (`pytest`, `npm test`, etc.)
- For processes that must stay alive across calls (dev servers, `--watch`, REPLs, debuggers), use the `shell` tool (`start`/`read`/`write`/`stop`/`list`) instead of `bash`
- `bash` is appropriate for git operations (use the `git` tool instead; safer)
- For GitHub operations (PRs, issues, checks), prefer the `gh` tool over raw bash or an MCP server

**Repository conventions:** when a task carries a `workspace`, agents auto-load that
repo's `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md` / `.cursorrules`
into context (`context/repo_instructions.py`). Honour them - they are the repo's house rules.
- `bash` is appropriate for environment inspection or one-off commands

---

## 17. Configuration

### 17.1 Single Settings Object

```python
# config/settings.py
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # required
    openrouter_api_key: str

    # paths
    north_home: Path = Path("~/.north").expanduser()

    # runtime
    north_env: Literal["development", "production", "test"] = "development"

    # tuning
    job_poll_interval_seconds: int = 5
    agent_read_timeout_seconds: int = 30
    task_cleanup_completed_days: int = 7
    task_cleanup_failed_days: int = 30
    confidence_increase_per_helpful_use: float = 0.05
    confidence_decrease_per_unhelpful_use: float = 0.03
    confidence_auto_approve_threshold: float = 0.8
    inference_pool_refresh_interval_hours: int = 6

    @property
    def secret(self) -> str:
        return (self.north_home / "secret.key").read_text().strip()

    @property
    def is_development(self) -> bool:
        return self.north_env == "development"

    @property
    def is_test(self) -> bool:
        return self.north_env == "test"

    model_config = {
        "env_file": ".env",
        "env_prefix": "NORTH_",
    }

settings = Settings()
```

### 17.2 User Settings vs. System Settings

Runtime environment config lives in `Settings` (Section 17.1) and is sourced from env vars / `.env`. User preferences that change at runtime (e.g. inference strategy) live in `NorthSettings` (`config/strategy.py`) and are persisted to `~/.north/settings.json`.

```python
# system config - read-only at runtime, set via env vars
from config.settings import settings
path = settings.north_home

# user preferences - mutable at runtime, changed via prompt or API
from config.strategy import NorthSettings, StrategyMode
north_settings = NorthSettings(settings.north_home / "settings.json")
north_settings.set_strategy(StrategyMode.ECO)
```

`NorthSettings` is created once in the lifespan and injected wherever needed (inference router, orchestrator). Never instantiate it inline inside a request handler.

### 17.3 Inference Strategy

Every `CompletionRequest` carries a `PoolPriority` signal. `ModelDispatcher` reads `NorthSettings.strategy` at call time to determine the model ordering:

- `eco` - cheapest priced model first, free models at tail
- `cruise` - tier matching `PoolPriority`, cross-tier fallback, free at tail (default)
- `sport` - most expensive first, free models at tail

Changing strategy takes effect on the next inference call. No restart required.

### 17.5 Never Read os.environ Directly

```python
# correct
from config.settings import settings
api_key = settings.openrouter_api_key

# wrong
import os
api_key = os.environ["OPENROUTER_API_KEY"]
```

---

## 18. Testing

### 18.1 Framework and Structure

`pytest` + `pytest-asyncio`. Tests mirror the source structure exactly.

```
tests/
  conftest.py          <- shared fixtures for all tests
  unit/
    ledger/
      test_sqlite_writer.py
    inference/
      test_dispatcher.py
      test_cost_tracker.py
    orchestrator/
      test_router.py
      test_failure_handler.py
      test_api_limits.py
      test_orchestrator_security.py
    agents/
      test_health_agent.py
      test_job_agent.py
      test_university_agent.py
      test_finance_agent.py
    tools/
      test_confidence_tracker.py
      test_tool_registry.py
    context/
      test_file_store.py
  integration/
    test_full_task_pipeline.py
    test_failure_and_recovery.py
    test_parallel_agents.py
```

### 18.2 Shared Fixtures in conftest.py

Fixtures used by more than one test file live in `tests/conftest.py`. Fixtures scoped to one module live in that module's `conftest.py`.

```python
# tests/conftest.py
import pytest
from pathlib import Path
from config.dependencies import Dependencies
# build_test_dependencies and MockInferenceRouter are defined in this file

@pytest.fixture
def deps(tmp_path: Path) -> Dependencies:
    return build_test_dependencies(tmp_path)

@pytest.fixture
def ledger(deps: Dependencies):
    return deps.ledger

@pytest.fixture
def context_store(deps: Dependencies):
    return deps.context_store
```

### 18.3 Test Behavior, Not Implementation

Tests assert on public interface behavior. If refactoring internals breaks a test, the test is wrong.

```python
# correct: tests the observable behavior
@pytest.mark.asyncio
async def test_ledger_write_records_completed_entry(ledger):
    entry = LedgerEntry(source=LedgerSource.PROMPT, input="test", status=LedgerStatus.COMPLETED)
    entry_id = await ledger.write(entry)
    retrieved = await ledger.get(entry_id)
    assert retrieved.status == LedgerStatus.COMPLETED
    assert retrieved.source == LedgerSource.PROMPT

# wrong: tests internal SQL
async def test_ledger_builds_correct_insert_sql():
    assert writer._build_sql() == "INSERT INTO ledger ..."
```

### 18.4 Agents Are Testable Without the Orchestrator

```python
@pytest.mark.asyncio
async def test_job_agent_produces_prep_plan(deps, tmp_path):
    agent = JobAgent(
        context_store=deps.context_store,
        task_context=TaskContextStore(tmp_path / "task_001.db"),
    )
    payload = AgentPayload(
        task_id="task_001",
        prompt="Help me prep for my first week at LinkedIn.",
        context="User starts LinkedIn internship June 2nd, distributed systems team.",
    )
    result = await agent.run(payload)
    assert isinstance(result, AgentResult)
    assert result.summary
    assert "checklist" in result.data or "output" in result.data
```

### 18.5 Mock External Dependencies

Never make real HTTP calls or write to real files in unit tests. Use dependency injection to inject test doubles.

```python
# tests/unit/inference/test_openrouter_router.py
class MockHttpClient:
    async def get(self, url: str, **kwargs) -> MockResponse:
        return MockResponse(json=SAMPLE_OPENROUTER_RESPONSE)

@pytest.mark.asyncio
async def test_router_selects_reasoning_pool_for_high_priority():
    router = build_router(openrouter_api_key="test")
    await router.refresh_pools()
    model = await router.get_model(PoolPriority.HIGH)
    assert model in EXPECTED_REASONING_MODELS
```

### 18.6 Test Naming

Test names are full sentences describing the behavior being tested.

```python
# correct
async def test_classifier_returns_consequential_for_booking_intent(): ...
async def test_ledger_write_records_completed_entry(): ...
async def test_agent_registry_raises_when_agent_not_found(): ...
async def test_parallel_agents_reconstruct_after_one_failure(): ...

# wrong
async def test_classifier(): ...
async def test_ledger(): ...
async def test_agents(): ...
```

### 18.7 No Tests for Trivial Code

Do not test one-line getters, simple wrappers, or direct pass-throughs. Test meaningful behavior.

### 18.8 Tests Land With the Code They Cover

Tests for new or modified functionality are written in the same change as the functionality itself. Adding code without adding or updating tests is an incomplete change. See Section 23.4 for the full rule.

---

## 19. Documentation

### 19.1 Docstrings on Every Public Class and Method

Every class and every public method in every `base.py` and `__init__.py` has a docstring. Private methods have docstrings only when the logic is non-obvious.

```python
class LedgerWriter(ABC):
    """Abstract interface for writing and reading the north event ledger.

    The Ledger is an append-only audit trail. Every event in the system
    writes an entry. Entries are never modified or deleted.

    Implementations: SQLiteLedgerWriter (production).
    """

    async def write(self, entry: LedgerEntry) -> str:
        """Write a new entry to the ledger.

        Args:
            entry: The entry to write. id and created_at are set by the implementation.

        Returns:
            The generated entry ID.

        Raises:
            LedgerWriteError: If the write fails.
        """
        ...
```

### 19.2 Module-Level Docstrings

Every module file starts with a one-line docstring explaining what the module does.

```python
"""SQLite implementation of the LedgerWriter interface."""

"""Abstract base class and interface for all north agents."""

"""OpenRouter-backed implementation of the InferenceRouter interface."""
```

### 19.3 README in Every Agent Folder

Every agent folder has a `README.md` that explains what the agent does, what tools it uses, and how to test it locally.

```
agents/job/README.md
agents/health/README.md
agents/university/README.md
agents/finance/README.md
```

---

## 20. Git

### 20.1 Commit Message Format

Every commit message follows the `Release x.y.z: description` format - a single line, no body required.

```
Release {version}: {what changed in plain english}

Examples:
Release 1.3.5: coding enhancements + three-layer BashTool safety + pre-commit checklist
Release 1.3.4: fix JudgementFilter duplicate construction at startup
Release 1.3.3: multi-provider inference with Groq and Gemini routers
Release 1.3.2: semantic tool selection and atomic fact store
```

**Rules:**
- Version must match `pyproject.toml` exactly at the time of commit.
- Description is plain English - no angle brackets, no conventional-commit prefixes.
- One line only. The "why" lives in `CHANGELOG.md`, not in the commit body.


### 20.2 One Concern Per Commit

One logical change per commit. Never mix a refactor with a feature. Never mix a bug fix with a style change.

### 20.3 Branch Naming

```
feature/{module}-{description}    feature/ledger-agent-output-field
fix/{module}-{description}        fix/inference-pool-refresh-timeout
refactor/{module}-{description}   refactor/agents-template-method
docs/{description}                docs/add-agent-readme-files
```

### 20.4 .gitignore from Day One

```
# python
.venv/
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.egg-info/
dist/
build/

# tools
.ruff_cache/
.pytest_cache/
.mypy_cache/

# env and secrets
.env
.env.*
!.env.example

# os
.DS_Store
Thumbs.db

# editor
.idea/
.vscode/
*.swp
```

`~/.north/` lives entirely outside the repository. It is never committed under any circumstances.

---

## 21. Open Source Standards

north is public. Every file, every interface, every decision must be understandable by someone reading the codebase for the first time.

### 21.1 Required Root Files

```
north/
  README.md              <- project overview, quickstart, architecture diagram link
  CONTRIBUTING.md        <- how to add an agent, how to add a tool, how to run tests
  LICENSE                <- MIT license
  CHANGELOG.md           <- version history with dates
  CODE_OF_CONDUCT.md     <- standard contributor covenant
  SECURITY.md            <- how to report a vulnerability
  .env.example           <- all env vars with descriptions and example values, no real secrets
  pyproject.toml         <- all dependencies, scripts, tool config
```

### 21.2 .env.example Is Always Up to Date

Every environment variable must be in `.env.example` with a comment explaining what it does.

```bash
# .env.example

# Required: your OpenRouter API key for LLM access
# Get one at https://openrouter.ai/keys
NORTH_OPENROUTER_API_KEY=sk-or-your-key-here

# Optional: directory where north stores all data (default: ~/.north)
# NORTH_NORTH_HOME=~/.north

# Optional: development or production (default: development)
# NORTH_NORTH_ENV=development

# Optional: how often the job processor polls for new jobs in seconds (default: 5)
# NORTH_JOB_POLL_INTERVAL_SECONDS=5
```

### 21.3 CONTRIBUTING.md Covers the Three Key Flows

1. How to add a new agent (folder structure, required files, config schema, how to test)
2. How to add a new tool (implement `Tool`, drop it in the right `tools/` subdir for auto-discovery, list it in an agent's `tools.yaml` if specialized)
3. How to run the full test suite and what passing looks like

### 21.4 No Exposed Secrets

Never commit API keys, tokens, or the `secret.key` file. The `secret.key` is generated locally on first run. API keys come from environment variables only.

### 21.5 Versioning: Semantic Versioning

`MAJOR.MINOR.PATCH` following semver.org.

- MAJOR: breaking change to the agent interface, tool interface, or Ledger schema
- MINOR: new feature, new agent, new tool, new API endpoint
- PATCH: bug fix, documentation update, dependency update

Version is declared in `pyproject.toml` and in `CHANGELOG.md`.

---

## 22. What Not to Build

Do not add the following unless the spec explicitly requires it. If it feels necessary, re-read the spec first.

- Caching layers
- Generic retry middleware (failure handling is specified in the system spec)
- Third-party logging frameworks (structlog, loguru) - the Ledger is the log
- Event bus or pub/sub systems - Ledger writes are the observer pattern
- Plugin lifecycle hooks - agents register via the folder system
- Health check or metrics endpoints
- Global mutable state anywhere except `config/dependencies.py`
- Singletons - use dependency injection
- Abstract base classes beyond the interface map in Section 6.1
- God classes - if a class is growing, split it
- Premature optimization - profile first, optimize second

---

## 23. Working with Claude Code

Sections 1–22 cover *what* to write. This section covers *how* to land it. Applies equally to Claude Code and human contributors.

### 23.1 If Unsure, Ask

When confused - about intent, scope, the right interface, which dependency, or where code belongs - stop and ask. Guessing is unacceptable.

**Ask when:** the request reads more than one way; the answer depends on a fact not in spec, code, or memory; the decision touches an architectural seam (new ABC, new module, new top-level dir, new public-interface field); or a choice would lock in an unapproved dependency.

**Ask well:** present 2–4 concrete options with trade-offs. "What do you want?" with no options is not a real question.

### 23.2 Confirm Before Each Substantive Change

Surface substantive changes and wait for confirmation. "Substantive" = anything beyond a typo fix or an edit directly instructed in the current message.

**Exception - similar-pattern batching:** once the user has approved a kind of change (rename a symbol, apply a lint rule, the same refactor across modules), apply it to other obvious cases without re-asking. If borderline, ask.

### 23.3 Tech Decisions: Research, Reason, Propose, Apply

No new library, framework, service, or CLI tool enters north without these four steps in order:

1. **Research** - 2–4 real alternatives, current docs, maintenance status, license.
2. **Reason** - case-for and case-against per option, ending in the trade-off that drives the recommendation.
3. **Propose** - present to the user as a question. Do not start coding against the new tech.
4. **Apply** - on confirmation, update README Section 16, `pyproject.toml`, and this file if a new convention is needed. Then start using it.

One canonical tool per job. Adding `requests` next to `httpx`, or `unittest` next to `pytest`, is a regression even if the new one is good in isolation.

### 23.4 Tests Are Currently Deferred

Do not write tests when adding new functionality. The pytest harness and the existing test suite (~426 tests) stay in place; Section 18 stays in place as the convention for when this policy is lifted.

**Why:** Build-speed during the pre-MVP phase, while module shape is still moving and the cost of keeping tests synchronized outweighs their value.

**How to apply:** Skip writing test files for new modules. Skip updating tests during refactors unless an existing test breaks - then fix it. CHANGELOG entries no longer include "Tests under …" paragraphs.

### 23.5 Every Change Updates CHANGELOG.md

Every change - feature, fix, refactor, doc edit, dependency bump - adds an entry to the `[Unreleased]` section of `CHANGELOG.md`, in [Keep a Changelog](https://keepachangelog.com) format. Subheadings: `Added`, `Changed`, `Fixed`, `Removed`, `Deprecated`, `Security`. One scannable line per change; the "why" lives in the commit message.

On a release, the contents of `[Unreleased]` move under a new `## [x.y.z] - YYYY-MM-DD` heading and `[Unreleased]` is reset to empty subheadings (semver per Section 21.5).

A change that touches code without updating the changelog is incomplete.

### 23.6 New Standards Land in This File

When the user states a rule of practice - "always X", "never Y", "from now on Z" - capture it here in the most relevant existing section before acting on it. Keep entries terse: state the rule, give a one-line **Why**, give a one-line **How to apply**. Skip worked examples unless the rule cannot be understood without one. Adding to this file is not a license to expand it.

### 23.7 Pre-Commit Checklist

Every commit is incomplete until all four obligations below are satisfied. Check them in order before writing the commit message.

**Why:** Code that ships without a changelog entry, stale docs, or a mismatched version tag creates invisible technical debt - future contributors (and you, six months later) cannot tell what changed or why.

**Checklist:**

| # | Obligation | Files to update | Done when… |
|---|---|---|---|
| 1 | **Changelog** | `CHANGELOG.md` | The change has a one-line entry under `[Unreleased] > Added / Changed / Fixed / …` (§23.5) |
| 2 | **Version** | `pyproject.toml`, `uv.lock` (`uv lock`) | `pyproject.toml` version matches the semver rule (§21.5); `uv.lock` re-generated so it reflects the same version |
| 3 | **Docs** | `docs/TECHNICAL_FEATURES.md` or `docs/ARCHITECTURE.md` | Any new architectural pattern, design decision, or non-obvious behaviour is documented in the right doc file |
| 4 | **Commit message** | - | Follows the format in §20.1 and names the module(s) touched |

**How to apply:**

- Run through the checklist top-to-bottom, not bottom-to-top.
- A change that adds no new behaviour (pure typo fix, import sort) may skip obligations 2 and 3 but never obligation 1.
- If a doc section for the new behaviour does not exist yet, create it - do not append to an ill-fitting section.
- `uv lock` is the only acceptable way to update `uv.lock`; never hand-edit it.

**What "complete" looks like for a typical feature commit:**

```
# 1. CHANGELOG.md - entry added under [Unreleased]
## [Unreleased]
### Added
- BashTool three-layer command safety: instant bypass for read-only commands, …

# 2. pyproject.toml version bumped; uv lock re-run
version = "1.3.5"   # was 1.3.4
$ uv lock           # regenerates uv.lock

# 3. docs/TECHNICAL_FEATURES.md updated
## 13. Three-Layer BashTool Command Safety
…

# 4. Commit message
Release 1.3.5: coding enhancements + three-layer BashTool safety + pre-commit checklist
```