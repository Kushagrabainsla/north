# north: System Specification
### Personal Life Operating System
> Version 1.2 · May 2026

---

## Table of Contents

1. [Vision](#1-vision)
2. [System Overview](#2-system-overview)
3. [Perception Layer](#3-perception-layer)
4. [The Ledger](#4-the-ledger)
5. [Context Layer](#5-context-layer)
6. [The Orchestrator](#6-the-orchestrator)
7. [Agent Layer](#7-agent-layer)
8. [Inference Router](#8-inference-router)
9. [Approval Layer](#9-approval-layer)
10. [Interface Model](#10-interface-model)
11. [Cron and Async Jobs](#11-cron-and-async-jobs)
12. [Storage Model](#12-storage-model)
13. [End-to-End Data Flow](#13-end-to-end-data-flow)
14. [Repository Structure](#14-repository-structure)
15. [Open Questions](#15-open-questions)
16. [Tech Stack](#16-tech-stack)

---

## 1. Vision

Most of what we call work in daily life is not real thinking. It is coordination overhead. Planning a week, tracking finances, managing academic deadlines, preparing for an internship: none of this requires you specifically. It just requires context about you.

north is a personal AI operating system that runs continuously in the background. You give it a north star (what you want to achieve, who you want to become) and it handles the operational work across every domain of your life. You review, approve, and enjoy the output. The cognitive load of coordination disappears.

**Core principle:** You should spend your time thinking, deciding, and experiencing, not managing. north manages so you do not have to.

---

## 2. System Overview

north is built from eight distinct layers. Each has one clear job. Together they form a pipeline from raw input to real-world execution on your behalf.

```
+-----------------------------------------------------+
|                        YOU                          |
|          Voice (dictation key) · Text prompt        |
+------------------------+----------------------------+
                         |
+------------------------v----------------------------+
|                 PERCEPTION LAYER                    |
|          Dictation key (Whisper) · Keyboard input   |
+------------------------+----------------------------+
                         |
                         | direct routing (not through Ledger)
                         |
+------------------------v----------------------------+
|                   ORCHESTRATOR                      |
|       Classifier -> North star check -> Route       |
+-------+------------------+------------------+-------+
        |                  |                  |
+-------v------+  +--------v-----+  +---------v----+  +---------+
|    Health    |  | University   |  |     Job      |  | Finance |
+--------------+  +--------------+  +--------------+  +---------+
        |                  |                  |              |
        +------------------+------------------+--------------+
                           |
                  Task Context Object (SQLite)
                           |
+------------------------v----------------------------+
|                  APPROVAL LAYER                     |
|      macOS notifications · Three card types         |
+------------------------+----------------------------+
                         |
+------------------------v----------------------------+
|               REAL-WORLD EXECUTION                  |
+-----------------------------------------------------+
         ^ feedback loop back into context layer

+-----------------------------------------------------+
|                    THE LEDGER                       |
|   Every layer writes events here asynchronously     |
|   Append-only · SQLite · Never blocks the main path |
+-----------------------------------------------------+

+-----------------------------------------------------+
|                  CONTEXT LAYER                      |
|   Built by extraction pipeline reading the Ledger   |
|   public · private · privacy rules ·                |
|   judgement rules · north stars                     |
+-----------------------------------------------------+
```

**Critical data flow note:** Input goes directly from the Perception Layer to the Orchestrator. The Ledger is not in the request path. Every layer writes to the Ledger asynchronously as a side effect. The Ledger feeds the Context Layer via the extraction pipeline, which runs as a background job. The Orchestrator reads from the Context Layer at the start of each task, not from the Ledger directly.

---

## 3. Perception Layer

The Perception Layer is how north receives input. In v1, all input is explicit and user-initiated. Nothing is captured passively or ambiently.

### 3.1 Voice Input: Dictation Key

A configurable push-to-talk hotkey triggers audio capture. The user speaks, releases the key, and the captured audio is sent to OpenRouter's transcription endpoint (`POST /api/v1/audio/transcriptions`) using the same `NORTH_OPENROUTER_API_KEY` north already uses for LLM inference. The transcribed text is then routed to the Orchestrator via `POST /orchestrator/task`.

- Cloud transcription via OpenRouter (Section 8), one provider and one API key for both LLM and STT
- No wake word, no ambient capture, no continuous mic
- Every transcription is intentional and user-initiated
- Transcribed text is treated identically to keyboard input downstream
- The capture hotkey is configurable and intentionally **not** `Fn`, which is reserved for macOS's built-in Dictation. Default: `Right Option + Space` (configurable in `~/.north/settings.toml`).

The trade-off is explicit: audio leaves the machine in exchange for sub-second transcription latency and a single-provider operational story. The `Notifier`-style pattern (`docs/CODING_STYLE.md` Section 6.1) keeps a future local fallback (e.g. `mlx-whisper`) cheap to add if local-first ever becomes a requirement again.

### 3.2 Text Input: Keyboard Prompt

The user types a prompt directly into the Web UI or CLI and submits it. Routes directly to the Orchestrator via the same endpoint.

- Available via Web UI input field
- Available via CLI: `north task "your prompt here"`
- Both paths hit `POST /orchestrator/task`

### 3.3 What Is Out of Scope for v1

- Screen capture
- Ambient microphone recording
- Browser extension and DOM parsing
- Mobile microphone input
- Wake word detection
- Native app integrations (Gmail, Canvas, etc.)

These are deliberately deferred. v1 perception is intentional input only.

---

## 4. The Ledger

The Ledger is the system's complete, permanent audit trail. Every event that happens in north (whether triggered by the user, a cron job, an async job, or an agent) writes an entry to the Ledger asynchronously. The Ledger write never blocks the main request path. Nothing is deleted by default.

### 4.1 Purpose

The Ledger serves three jobs simultaneously:

1. **Audit trail:** complete history of everything north did and why
2. **Failure recovery:** granular per-agent entries with full structured output allow partial task reconstruction without wasting tokens
3. **Context source:** the extraction pipeline reads the Ledger to build and update the Context Layer

### 4.2 Schema

```sql
CREATE TABLE ledger (
  id              TEXT PRIMARY KEY,
  timestamp       DATETIME NOT NULL,
  source          TEXT NOT NULL,
    -- full enum defined in Section 4.3
  task_id         TEXT,              -- links to a task if part of one
  agent           TEXT,              -- which agent wrote this entry (if applicable)
  input           TEXT,              -- what triggered this event
  action          TEXT,              -- what the system did
  output          TEXT,              -- human-readable summarized result for UI display
  agent_output    JSON,              -- full structured agent output for Task Context reconstruction on failure
  tools_used      JSON,              -- list of tools called during this event
  model_used      TEXT,              -- which LLM was used
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  cost_usd        REAL,
  status          TEXT,              -- completed | pending | failed | approved | rejected | cancelled
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

**Two output fields are intentional.** `output` is a human-readable summary shown in the Ledger viewer in the Web UI. `agent_output` is the full structured JSON the agent produced, used exclusively for reconstructing the Task Context Object during failure recovery. Both are written together when an agent completes its subtask.

### 4.3 Source Field: Complete Enum

The `source` field must be exactly one of the following values. All writers across the entire codebase must use these exact strings. No other values are valid.

```
prompt              user typed a prompt via CLI or Web UI
mic                 user spoke via dictation key, Whisper transcribed
cron                job triggered by the cron scheduler
agent               an agent writing its own completion entry
async               an async or retry job triggered by the job processor
system              internal system events (startup, shutdown, config change,
                    classifier decisions, routing decisions, north star checks,
                    extraction pipeline writes, tool confidence updates)
manual_injection    user fed context via file, text, or URL
inference_router    inference cost and model usage logging per API call
approval            user approved, rejected, or answered a question card
```

### 4.4 Write Rules

- Every agent writes its own entry immediately upon completing its subtask, not when the whole task completes
- The Orchestrator writes entries (source: system) for routing decisions, north star checks, and classifier decisions
- The Orchestrator writes entries (source: system) for every inter-agent data request it mediates
- The job processor writes entries for every cron and async job execution
- The Approval Layer writes entries (source: approval) for every approval, rejection, and question answered
- The Inference Router writes entries (source: inference_router) for every inference call
- Entries are never modified after writing, only new entries are added
- All Ledger writes are asynchronous and non-blocking

### 4.5 Granularity

One entry per agent per task, not one entry per task. This is critical for failure recovery. If three agents are running and one fails, the two completed agents' full outputs are preserved in `agent_output` in the Ledger and can be reconstructed into a partial Task Context Object without re-running them.

---

## 5. Context Layer

The Context Layer is a structured, persistent model of the user: goals, preferences, constraints, decision patterns, and north stars. Every agent reads from it. It starts empty and grows naturally from usage. No onboarding, no structured intake.

### 5.1 Storage

Five markdown files. Human-readable, directly editable, git-versioned.

```
context/
  public.md            <- who you are. read by all agents.
  private.md           <- sensitive details. local only. never leaves machine.
  privacy_rules.md     <- who can access what, and under what conditions.
  judgement_rules.md   <- how you decide. confidence-scored. self-updating.
  north_stars.md       <- what you are working toward, across all time horizons.
```

### 5.2 The ContextStore Interface

Nothing in the system reads or writes context files directly. Everything goes through the `ContextStore` interface. This makes the storage backend swappable: files today, database tomorrow, without changing any other layer.

```python
from abc import ABC, abstractmethod

class ContextStore(ABC):
    @abstractmethod
    def read(self, document: str) -> str:
        """Read a full context document by name."""
        ...

    @abstractmethod
    def write(self, document: str, content: str) -> None:
        """Overwrite a context document entirely."""
        ...

    @abstractmethod
    def append(self, document: str, delta: str) -> None:
        """Append a delta to a context document."""
        ...

    def search(self, query: str) -> str:
        """Semantic search over context. Not implemented in v1.
        Do not call this method in v1 code. It will raise NotImplementedError.
        Upgrade to DBContextStore when semantic search is needed.
        """
        raise NotImplementedError(
            "search() is not available in v1. "
            "Upgrade to DBContextStore when context files exceed context window limits."
        )


class FileContextStore(ContextStore):
    def __init__(self, base_path: str = "~/.north/context"):
        self.base_path = base_path

    def read(self, document: str) -> str:
        with open(f"{self.base_path}/{document}") as f:
            return f.read()

    def write(self, document: str, content: str) -> None:
        with open(f"{self.base_path}/{document}", "w") as f:
            f.write(content)

    def append(self, document: str, delta: str) -> None:
        with open(f"{self.base_path}/{document}", "a") as f:
            f.write(f"\n{delta}")
```

To swap the backend in the future:

```python
# One line change in config. Nothing else in the system changes.
context_store = DBContextStore()  # was FileContextStore()
```

### 5.3 The Five Documents

#### public.md
General facts about the user freely available to all agents. Goals, preferences, schedule patterns, dietary habits, risk appetite, professional background. Updated continuously by the extraction pipeline.

#### private.md
Sensitive information agents cannot read automatically. Specific account numbers, medical details, relationship dynamics. When an agent needs it, it raises a request through the Orchestrator. The user approves via an Approval card. That session uses the data and closes cleanly. **Stored locally only. Never leaves the machine.**

#### privacy_rules.md
Edited directly by the user. Defines which agents can request private context, which have automatic access, and which topics always route to private context. Also defines trust thresholds per action category used by the Approval Layer.

#### judgement_rules.md
A living document that writes itself entirely from watching the user make decisions. Never edited directly by the user or agents. Every approval, rejection, and answered question writes a delta to it. Confidence-scored.

```
Finance:
  - Willing to take higher risk on small cap tech          [confidence: 8/10]
  - Never approves anything over 50,000 without breakdown  [confidence: 10/10]

Health:
  - Prefers morning workouts before 9am                    [confidence: 6/10]
  - Approves high-protein meal plans automatically         [confidence: 7/10]
```

Each rule starts at confidence 1/10 (one observation = hypothesis). Confidence rises with each confirmation and decays slightly with each contradiction. The Orchestrator treats rules at 8/10 or above as reliable preferences eligible for auto-approval.

#### north_stars.md
Goals across every time horizon simultaneously. Every consequential action checks this document before proceeding.

```
Lifetime:   Financial independence, meaningful technical work at scale
5-year:     Principal or Staff engineer at a top infrastructure company
1-year:     Crush LinkedIn internship, publish ML research paper
3-month:    Ship north v1, complete CS271 with strong grade
This week:  Finish architecture spec, start Phase 2 of hallucination project
```

When two north stars conflict, the Orchestrator surfaces the tension to the user rather than resolving it silently.

### 5.4 Extraction Pipeline

A background job runs periodically (every few minutes) reading new Ledger entries and asking one question about each: does this tell me something new, meaningful, and durable about this person?

Since all Ledger entries in v1 come from intentional user input (not ambient capture), the signal-to-noise ratio is already high. The extraction pipeline uses a cheap fast model (high_volume pool) and writes deltas to the appropriate context documents via `ContextStore.append()`. Every write is logged to the Ledger with `source: system`.

```
New Ledger entry (source: mic):
  "I always prefer window seats, book one for my next trip"

Extraction pipeline:
  -> meaningful preference detected
  -> appends to judgement_rules.md:
       "Always prefers window seat on flights [confidence: 1/10]"
  -> Ledger write: source=system, action="extraction: judgement_rules.md updated"
```

### 5.5 Manual Context Injection

Users can feed north information directly without waiting for the extraction pipeline to learn it naturally. All three paths are logged to the Ledger with `source: manual_injection` and then processed by the same extraction pipeline.

**Via CLI:**
```bash
north context add --file resume.pdf
north context add --file sjsu_transcript.pdf
north context add --text "My LinkedIn internship starts June 2nd, team is distributed systems"
north context add --url "https://linkedin.com/jobs/view/123456"
```

**Via Web UI:**
- Drag and drop file upload
- Text input field for direct facts
- URL field for web content ingestion

The extraction pipeline decides which context document each piece of information belongs to.

### 5.6 Context Viewer

The Web UI exposes a context viewer where the user can:
- Read all five context documents
- Edit them directly (writes via `ContextStore.write()`)
- See what the extraction pipeline added and when (Ledger filtered by `source: system`)
- Delete or correct wrong extractions

Full visibility and control over what north knows.

---

## 6. The Orchestrator

The Orchestrator is the brain of north. It sits between the Perception Layer and the Agent Layer. Its job: receive intent, read context, check alignment, decompose into work, coordinate parallel execution, manage shared state, and handle failure.

### 6.1 Request Flow

Every input goes through four stages in order:

```
Input arrives directly from Perception Layer (voice or text)
       |
       v
Stage 1: Classifier (high_volume pool: fast, cheap)
  -> trivial or consequential?
  -> trivial:       skip to Stage 3
  -> consequential: proceed to Stage 2
  -> Ledger write: source=system, action="classified as [trivial|consequential]"
       |
       v
Stage 2: North Star Check (reasoning pool)
  -> reads north_stars.md via ContextStore
  -> aligns:    proceed to Stage 3
  -> conflicts: surface tension card to user, await decision before continuing
  -> Ledger write: source=system, action="north_star_check: [aligned|conflict]"
       |
       v
Stage 3: Routing Decision (reasoning pool)
  -> reads public.md and agent registry via ContextStore and filesystem
  -> decides which agents are needed
  -> identifies dependencies between agents
  -> creates parallel execution groups
  -> creates Task Context Object (new SQLite file for this task_id)
  -> Ledger write: source=system, action="routed", agents=[...]
       |
       v
Stage 4: Parallel Execution
  -> spins up agent groups in dependency order
  -> manages shared state via Task Context Object
  -> assembles final output when all groups complete
  -> routes output to Approval Layer
```

### 6.2 Classifier

A single LLM call (high_volume pool) categorizing incoming intent as trivial or consequential.

**Trivial:** informational queries, simple lookups, article summaries, grocery lists, meal plans. No north star check. Routes directly to the appropriate agent and surfaces an Information card when done.

**Consequential:** irreversible actions (booking, buying, deleting), time-consuming actions (blocks calendar), resource-consuming actions (costs money), goal-adjacent actions (directly touches north_stars.md). Goes through north star check. Requires an Approval card.

### 6.3 Trivial Task Output Path

Trivial tasks skip the north star check and the Approval card. After the agent completes:
- The result is written to the Ledger with `status: completed`
- An Information card notification is sent to the user
- The result is displayed in the Web UI activity feed
- No user action is required to proceed

### 6.4 North Star Check

A single reasoning pool call that reads `north_stars.md` and evaluates whether the requested task aligns with active goals across all time horizons. Checks bottom-up: this week first, then upward through 3-month, 1-year, 5-year, lifetime.

- Aligns: proceed to routing
- Conflicts: surface the specific tension to the user before any work is done. Never silently resolve a north star conflict.

Example tension surface notification:
```
"You asked to book a weekend trip. This falls during your north sprint week
(3-month goal: Ship north v1). Estimated cost is 18,000 against your savings
target. Do you want to proceed?"
  [Proceed anyway]  [Cancel]
```

### 6.5 Routing Decision

A single reasoning pool call that reads the agent registry (all folders present in `/agents`) and produces a structured execution plan. The registry is read at runtime. Adding a new agent folder makes it automatically available for routing without any Orchestrator code changes.

```json
{
  "task_id": "abc123",
  "intent": "Help me prep for my first week at LinkedIn",
  "parallel_groups": [
    ["job", "university"]
  ],
  "dependencies": {},
  "agents_involved": ["job", "university"]
}
```

Groups run in order. Within each group, agents run simultaneously. Dependencies are resolved between groups.

**Note on agent selection:** The Orchestrator selects agents strictly based on registered agent domains and task content. It does not invent agents or use agents from outside the registry. If a task involves a domain with no registered agent, the Orchestrator routes to the closest available domain and flags the gap in its output.

### 6.6 Task Context Object

The Task Context Object is a SQLite database scoped to a single `task_id`. It is the shared workspace for all agents in a task. Agents never communicate directly. All reads and writes go through the Orchestrator.

```sql
CREATE TABLE task_state (
  agent         TEXT NOT NULL,
  key           TEXT NOT NULL,
  value         JSON,
  status        TEXT,           -- pending | completed | failed | awaiting_input
  written_at    DATETIME,
  PRIMARY KEY (agent, key)
)
```

Each task gets its own SQLite file at `~/.north/tasks/task_{id}.db`.

**Locking model: standard database principles**

- **Shared lock (read):** multiple agents can read simultaneously. Blocks only if an exclusive lock is held.
- **Exclusive lock (write):** one writer at a time. Blocks all readers and writers until complete. Released immediately after write.
- SQLite WAL mode handles this natively. No custom locking implementation needed.

**Read request flow:**
```python
response = orchestrator.read(
  task_id="abc123",
  requesting_agent="job",
  key="finance.budget",
  timeout=30,       # seconds to wait if key not yet available
  required=True     # if False, agent proceeds with a logged assumption on timeout
)
```

If the key is not yet available:
- Orchestrator polls every 2 seconds until timeout
- On timeout: checks source agent status
  - still running: extend timeout, notify user
  - failed: trigger failure handling flow
  - completed but key missing: log error, notify user

**Write request flow:**
```python
orchestrator.write(
  task_id="abc123",
  agent="finance",
  key="budget",
  value=28000,
  status="completed"
)
```

**Question request flow:**
```python
orchestrator.ask(
  task_id="abc123",
  agent="job",
  question="Which role should I prioritize for LinkedIn internship prep?",
  options=["Distributed Systems", "ML Infrastructure", "Both equally"],
  blocks_execution=True
)
```

Every read, write, and question is logged to the Ledger with `source: system`.

**Task Context Object cleanup policy:**

Task SQLite files are cleaned up by a daily cron job (`task_context_cleanup`, runs at 3:00 AM) to prevent unbounded accumulation:
- `completed` or `cancelled`: deleted after 7 days
- `failed`: retained for 30 days (may be needed for debugging or retry)
- `pending` or `running` with no update in more than 24 hours: treated as stale, marked `failed`, retained for 30 days

Ledger entries for all tasks are retained permanently regardless of Task Context Object cleanup.

### 6.7 Failure Handling

When an agent fails, the Orchestrator classifies the failure before taking any action:

```
rate_limit    -> auto-queue with cooldown. no notification yet.
               -> silent retry after cooldown period
                  (cooldown duration read from Retry-After header in API error response)
               -> if retry succeeds: continues normally
               -> if retry fails again: notify user

timeout       -> retry once immediately
               -> if succeeds: continues normally
               -> if fails: notify user

api_error     -> retry once
               -> if succeeds: continues normally
               -> if fails: notify user

logic_error   -> notify immediately. do not retry.
context_error -> notify immediately. needs user input to resolve.
```

**Failure notification card:**
```
"Finance agent failed: rate limit resolves in approximately 2 minutes."
  [Retry Now]  [Queue for Later]  [Cancel Task]
```

- **Retry Now:** Orchestrator reads `agent_output` from Ledger for all completed agents, reconstructs Task Context Object, retries only the failed agent
- **Queue for Later:** creates async job in job queue with `retry_after` timestamp. Silent retry after cooldown. Sends notification on completion.
- **Cancel Task:** writes cancellation to Ledger with `status: cancelled`. Task closed.

**Token preservation:** because the Ledger stores `agent_output` (full structured JSON) per agent per task upon completion, a failure in a later agent never wastes work already done. The Orchestrator reconstructs partial Task Context Object state from the Ledger without re-executing completed agents.

### 6.8 Orchestrator REST API

The Orchestrator exposes a local REST API on `localhost:8000`. Both the CLI and Web UI are clients of this API. The notification callback server also calls this API when the user taps an action button.

All endpoints require the shared secret header `X-North-Secret: {secret}` (see Section 9.1).

```
POST   /orchestrator/task                -> submit a prompt
GET    /orchestrator/tasks               -> list all active tasks
GET    /orchestrator/task/{id}           -> get task status and output
DELETE /orchestrator/task/{id}           -> cancel a task

GET    /orchestrator/ledger              -> view ledger entries (paginated)
GET    /orchestrator/ledger?task_id=x    -> filter by task
GET    /orchestrator/ledger?agent=x      -> filter by agent
GET    /orchestrator/ledger?source=x     -> filter by source type

GET    /orchestrator/agents              -> list registered agents
POST   /orchestrator/agent/run           -> manually trigger an agent
POST   /orchestrator/agent/create        -> scaffold a new agent

GET    /orchestrator/context/{doc}       -> read a context document
PUT    /orchestrator/context/{doc}       -> overwrite a context document
POST   /orchestrator/context/add         -> manual context injection

GET    /orchestrator/jobs                -> list job queue (filterable by status)
POST   /orchestrator/jobs                -> create a job
DELETE /orchestrator/jobs/{id}           -> cancel a job

GET    /orchestrator/inference/costs     -> inference cost summary
GET    /orchestrator/inference/models    -> current model pool state

GET    /orchestrator/tools/confidence    -> tool confidence scores per agent

GET    /orchestrator/stream              -> SSE stream for real-time Web UI updates

POST   /orchestrator/approval/respond    -> receive approval decision from callback server
```

---

## 7. Agent Layer

Agents are domain specialists. Each knows one domain and operates only within it. They do not talk to each other directly. All communication goes through the Task Context Object managed by the Orchestrator.

### 7.1 v1 Agent Set

| Agent | Domain | Responsibilities |
|-------|--------|-----------------|
| Health | Health and wellness | Meal planning, grocery lists, dietary tracking, workout planning |
| University | Academic | Coursework, Canvas deadlines, research papers, professor communications, GPA tracking |
| Job | Career | LinkedIn internship tasks, professional communications, career goals, interview prep, applications |
| Finance | Personal finance | Budgeting, expense tracking, savings progress, investment research |

### 7.2 Folder Structure

Each agent is a self-contained folder dropped into `/agents`. The Orchestrator scans this directory on startup and hot-reloads when it detects new or modified folders.

```
/agents
  /health
    agent.py              <- core logic, LLM prompting, tool calls, output formatting
    config.yaml           <- declaration: domain, model pool, accepted tasks
    tools.yaml            <- tool edges with initial confidence scores
    prompts/
      system.md           <- system prompt defining agent personality and expertise
      templates/          <- task-specific prompt templates
    tests/
      test_agent.py       <- isolated agent tests (no Orchestrator required)
  /university/
  /job/
  /finance/
```

**config.yaml example:**
```yaml
agent: health
domain: health
model_pool: fast_cheap
similar_to: null        # set to another agent name to inherit its tool confidence scores
accepts:
  - meal_planning
  - grocery_list
  - dietary_tracking
  - workout_planning
output_format: structured_json
version: 1.0.0
```

**tools.yaml example:**
```yaml
tools:
  - name: web_search
    initial_confidence: 0.5
  - name: calendar_api
    initial_confidence: 0.7
  - name: nutrition_api
    initial_confidence: 0.8
```

### 7.3 Agent Registration

Two paths to register an agent. Both result in the same outcome: a folder in `/agents` that the Orchestrator picks up automatically.

**Path 1: Manual drop**
```bash
git clone https://github.com/someone/north-agent-legal ./agents/legal
# Orchestrator hot-reloads, agent immediately available for routing
```

**Path 2: Scaffold generator**
```bash
north agent create
```

Interactive prompt:
```
Agent name: legal
Domain: legal
Model pool (fast_cheap / reasoning): reasoning
Tools needed: web_search, document_reader
Tasks it accepts: contract_review, compliance_check

Created /agents/legal/
   agent.py        <- boilerplate logic ready to customize
   config.yaml     <- pre-filled from your answers
   tools.yaml      <- tools with default confidence scores
   prompts/
     system.md     <- LLM-generated starter prompt for legal domain
   tests/
     test_agent.py <- basic test scaffold
```

The generator uses a reasoning pool call to produce a reasonable starting `system.md` based on the declared domain and tasks. The agent has a running start, not a blank page.

### 7.4 Tool Graph

Tools are not assigned statically per agent. They exist in a directed graph where edges connect agents to tools. An agent traverses only its own edges, loading only the tools it needs into context. No token waste from irrelevant tool definitions.

**Graph structure:**

```
Tool Graph (directed):

health ──────────────────> nutrition_api
health ──────────────────> fitness_tracker
health ──────────────────> calendar_api <── university, job
health ──────────────────> web_search   <── job, finance, university

university ──────────────> canvas_api
university ──────────────> web_search
university ──────────────> calendar_api
university ──────────────> gmail_api   <── job, finance

job ─────────────────────> linkedin_api
job ─────────────────────> gmail_api
job ─────────────────────> calendar_api
job ─────────────────────> web_search

finance ─────────────────> market_data_api
finance ─────────────────> expense_tracker
finance ─────────────────> web_search
finance ─────────────────> gmail_api
```

Cross-domain tools (calendar_api, web_search, gmail_api) are graph nodes with multiple incoming edges. The agent traverses its own edges and picks up shared tools naturally through the graph structure.

**Context loading order:** when an agent spins up, it loads its tool definitions into context sorted by confidence score descending. Low confidence tools are only loaded if the specific task explicitly requires them. This keeps the agent's context window lean.

### 7.5 Confidence Scoring and Persistence

Every tool edge in the graph carries a confidence score from 0.0 to 1.0. Scores update after every tool use.

```python
if tool_was_helpful:
    new_confidence = min(1.0, current_confidence + 0.05)
else:
    new_confidence = max(0.0, current_confidence - 0.03)
```

**Persistence:** confidence scores are stored in `~/.north/tools.db`, a dedicated SQLite database. This is separate from the Ledger (event log) and separate from the Task Context Object (per-task scratch space). `tools.db` is the authoritative source for current confidence state.

```sql
CREATE TABLE tool_confidence (
  agent           TEXT NOT NULL,
  tool            TEXT NOT NULL,
  confidence      REAL NOT NULL DEFAULT 0.5,
  uses_total      INTEGER DEFAULT 0,
  uses_helpful    INTEGER DEFAULT 0,
  last_updated    DATETIME,
  PRIMARY KEY (agent, tool)
)
```

On Orchestrator startup, all confidence scores are loaded from `tools.db` into memory. Every tool use updates the in-memory score and writes the delta to `tools.db`. Every confidence update is also logged to the Ledger with `source: system`.

**New agent inheritance:** when a new agent declares `similar_to: health` in `config.yaml`, the Orchestrator copies confidence rows from the `health` agent in `tools.db` as the new agent's starting prior. Tools not present in the source agent start at `initial_confidence` from `tools.yaml`.

### 7.6 The If-Unsure-Ask Rule

Agents follow a clear decision hierarchy when they encounter ambiguity:

1. Check Context Layer and `judgement_rules.md` first. The answer is probably already there.
2. Make a reasonable default, proceed, and flag it clearly in the output for the user to override via the Approval card.
3. If the decision is consequential and no clear default exists: stop and raise a Question through `orchestrator.ask()`.

When an agent raises a question, it sets `status: awaiting_input` in its Task Context Object row. The Orchestrator surfaces a Question card. The user answers via notification buttons or the Web UI. The answer is written back to the Task Context Object, the agent resumes, and the answered question is appended to `judgement_rules.md` so it is never asked again.

---

## 8. Inference Router

The Inference Router selects the appropriate LLM for every inference call in the system. Fully dynamic: no hardcoded model names in application code, no static config file for model assignments. Model selection is automatic based on task priority.

### 8.1 OpenRouter

All inference goes through OpenRouter (openrouter.ai). Single API endpoint, single API key, access to all major models across all providers. OpenRouter handles availability routing and returns per-call cost data in the response.

### 8.2 Dynamic Model Pools

The router pulls the live model list from OpenRouter every 6 hours and automatically groups models into three tiers based on current pricing and published capability benchmarks.

```
reasoning pool:    top tier by capability-to-cost ratio
                   typical members: claude-sonnet, gpt-4o, gemini-1.5-pro, deepseek-r1

fast_cheap pool:   mid tier
                   typical members: claude-haiku, gpt-4o-mini, gemini-flash

high_volume pool:  cheapest available
                   typical members: gemini-flash, gpt-4o-mini, claude-haiku
```

Pools refresh automatically. When a new model releases or pricing changes, it enters the correct pool without any manual action.

**Pool refresh failure handling:** if the OpenRouter refresh call fails for any reason (network error, API outage), the router continues using the last successfully fetched pool. The last known pool is persisted to `~/.north/inference_cache.json` after every successful refresh. On Orchestrator startup, this cache is loaded before the first live refresh attempt. If no cache exists and the first refresh fails, the router falls back to a hardcoded minimal pool defined in `inference/fallback_pools.py`. In all fallback cases, a warning is logged to the Ledger with `source: system` and the Orchestrator continues accepting tasks normally.

### 8.3 Priority-Driven Model Selection

The router reads the task priority signal from the Orchestrator classifier and selects accordingly. No fixed model assignments per component.

```
high priority    -> reasoning pool (best available model)
medium priority  -> fast_cheap pool
low priority     -> high_volume pool (cheapest available)
```

**Priority signals:**
- Consequential task (from classifier): high priority
- Background, async, or non-blocking task: low priority
- Everything else: medium priority

**Component defaults at v1:**
```
orchestrator routing      -> high priority
north star check          -> high priority
finance agent             -> high priority (consequential domain)
job agent                 -> high priority (consequential domain)
university agent          -> medium priority
health agent              -> medium priority
extraction pipeline       -> low priority (background job)
judgement rules writer    -> low priority (background job)
classifier                -> low priority (simple binary classification)
```

### 8.4 Automatic Fallback

On rate limit or API error for any model, the router automatically selects the next available model in the same pool. The calling agent never knows which model it is using and never stops executing.

```
finance agent -> requests reasoning pool -> claude-sonnet rate limited
             -> router tries gpt-4o
             -> if gpt-4o also rate limited -> tries gemini-1.5-pro
             -> agent continues transparently
```

### 8.5 Cost Tracking

Every inference call is logged to the Ledger with `source: inference_router`:

```
source:     inference_router
component:  finance_agent
priority:   high
model_used: anthropic/claude-sonnet
tokens_in:  1240
tokens_out: 380
cost_usd:   0.0024
task_id:    abc123
```

Cost summary available via CLI and Web UI:
```bash
north inference costs --period week
north inference costs --period month
north inference costs --agent finance
north inference models           # show current pool state
```

### 8.6 Audio Transcription

The Inference Router also owns audio transcription via OpenRouter's `POST /api/v1/audio/transcriptions` endpoint (see Section 16.6). The same client, the same `NORTH_OPENROUTER_API_KEY`, the same fallback semantics, and the same Ledger logging (`source: inference_router`) apply. Transcription is a separate code path from chat-completion (different endpoint, different request shape) but shares all infrastructure.

Default transcription model: `groq/whisper-large-v3`. The Inference Router exposes a configurable override the same way it exposes LLM model selection.

---

## 9. Approval Layer

The Approval Layer is the primary interface between north and the user for consequential outputs. Users do not interact with agents directly. They interact with macOS notifications and the Web UI.

### 9.1 macOS Native Notifications and Security

The Approval Layer sends macOS native notifications with action buttons. A local callback server runs on `localhost:8001` and receives button taps from macOS.

**Security:** the notification callback server is secured with a shared secret generated at first startup and stored at `~/.north/secret.key`. Every notification payload embeds this secret in the callback URL or request body. Every callback request to `localhost:8001` must include the `X-North-Secret` header with the correct value. Requests without a valid secret are rejected with HTTP 403. This prevents any other local process from faking an approval action.

The same shared secret is required on all calls to the Orchestrator REST API (Section 6.8).

```
Agent completes work
-> Approval Layer creates card payload
-> Sends macOS notification with action buttons
   (callback URL contains the shared secret)
-> User taps action button
-> macOS sends POST to localhost:8001/callback with secret
-> Callback server validates secret
-> Forwards decision to POST /orchestrator/approval/respond
-> Orchestrator writes to Ledger: source=approval, status=[approved|rejected]
-> judgement_rules.md updated via extraction pipeline
-> System executes approved action or cancels rejected one
```

### 9.2 Three Card Types

**Information cards:** completed autonomous work requiring no action.
```
"Your weekly meal plan is ready."
"Portfolio summary: up 2.3% this week."
  [View Detail]
```

**Approval cards:** consequential actions requiring explicit sign-off before execution.
```
"Book flight SFO to NRT for 32,000. June 12 to 19."
  [Approve]  [Reject]  [View Detail]
```

**Question cards:** genuine ambiguities the agent could not resolve from context.
```
"Do you prefer to stay in Shinjuku or Shibuya for the Japan trip?"
  [Shinjuku]  [Shibuya]  [View Detail]
```

### 9.3 View Detail

For complex outputs that do not fit in a notification (full itineraries, research summaries, meal plans), the [View Detail] button opens the full card in the Web UI on the second monitor. The user can approve, reject, or answer questions directly from the Web UI without interacting with the notification.

### 9.4 Judgement Rules Filtering

Before surfacing any card, the Orchestrator checks `judgement_rules.md`. If a rule clearly covers the situation at confidence 8/10 or above, it auto-approves, auto-rejects, or pre-fills a recommendation. Cards only surface when the situation is novel or when no high-confidence rule covers it. Over time the approval layer gets quieter.

### 9.5 Trust Thresholds

Configurable per action category in `privacy_rules.md`:

```
low_stakes_repeatable:    auto-approve  # grocery list, meal plan, article summary
medium_stakes:            notify        # calendar changes, research outputs
high_stakes_irreversible: always ask    # money movement, bookings, commitments
```

High-stakes-irreversible actions always surface an Approval card regardless of judgement rule confidence. This is a hard override that cannot be bypassed by learned rules.

### 9.6 Feedback Loop

Every approval, rejection, and answered question is processed by the extraction pipeline and appended to `judgement_rules.md`. The system learns from every interaction and asks less over time.

---

## 10. Interface Model

north has two primary interfaces. Both talk to the same Orchestrator REST API. The CLI is direct access. The Web UI makes HTTP calls to the same endpoints.

### 10.1 Web UI: Second Monitor Dashboard

A local web UI served by the Orchestrator at `localhost:8000/ui`. Server-rendered Jinja2 templates with HTMX for interactivity. No separate frontend process and no build step. Intended to run permanently on a second monitor, giving continuous visibility into everything north is doing.

**Three panels:**

**Live Activity Feed**
Real-time stream of Orchestrator activity via SSE (`GET /orchestrator/stream`). Every agent action, tool call, Ledger write, and job execution appears as it happens. Each event shows source, agent, action, and status.

**Approval Surface**
Full card rendering for complex approvals and questions. Approve, reject, and answer questions directly here without opening a notification. All cards that have been sent as notifications are also mirrored here.

**Control Panel**
- Submit text prompts to the Orchestrator
- View and edit all five context documents
- Browse Ledger history with filters (source, agent, task ID, date range)
- Manage registered agents (view config, enable, disable)
- View job queue with filters (status, agent, type) and cancel controls
- View inference cost breakdown by period and agent
- Manual context injection (file upload, text input, URL ingestion)
- Tool confidence scores per agent (read-only view)

### 10.2 CLI: Control Plane

Direct terminal access to the Orchestrator. Every command maps to a REST API call.

```bash
# Task management
north task "Plan my week"
north task "What assignments are due this week?"
north tasks                              # list active tasks
north task cancel {id}

# Context management
north context view public
north context view north_stars
north context edit judgement_rules       # opens in $EDITOR
north context add --file resume.pdf
north context add --text "I prefer mornings for deep work"
north context add --url "https://example.com/article"

# Agent management
north agent list
north agent create
north agent run health --task "meal plan for today"

# Ledger
north ledger                             # recent entries
north ledger --task {id}
north ledger --agent finance
north ledger --source manual_injection
north ledger reprocess --from 2026-05-01 # rerun extraction pipeline from date

# Job queue
north jobs                               # list all jobs
north jobs --status pending
north job cancel {id}

# Inference
north inference costs --period week
north inference costs --agent finance
north inference models

# Tools
north tools confidence --agent health

# Config
north config set ledger.retention_days 90
north config set jobs.poll_interval_seconds 5
```

---

## 11. Cron and Async Jobs

north executes work autonomously on a schedule and in response to events. All jobs, regardless of how they were created, flow through a single persistent SQLite job queue.

### 11.1 Job Queue Schema

```sql
CREATE TABLE job_queue (
  job_id          TEXT PRIMARY KEY,
  type            TEXT NOT NULL,    -- cron | event | async | retry
  agent           TEXT NOT NULL,
  task            TEXT NOT NULL,
  payload         JSON,
  status          TEXT NOT NULL,    -- pending | running | completed | failed | cancelled
  priority        INTEGER,          -- 1 (high) | 2 (medium) | 3 (low)
  scheduled_at    DATETIME,         -- when to run
  started_at      DATETIME,
  completed_at    DATETIME,
  retry_count     INTEGER DEFAULT 0,
  max_retries     INTEGER DEFAULT 3,
  retry_after     DATETIME,         -- for rate limit cooldown periods
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

The queue persists in SQLite at `~/.north/jobs.db`. If the Orchestrator goes down, it reads pending jobs on restart and resumes exactly where it left off. No jobs are lost.

### 11.2 Job Processor

Runs continuously as part of the Orchestrator. Polls `jobs.db` every 5 seconds (configurable via `north config set jobs.poll_interval_seconds`).

```
Job processor loop:
-> SELECT * FROM job_queue
   WHERE status = 'pending'
   AND scheduled_at <= now
   AND (retry_after IS NULL OR retry_after <= now)
   ORDER BY priority ASC, scheduled_at ASC
-> for each eligible job:
     -> UPDATE status = 'running', started_at = now
     -> spin up declared agent with job payload
     -> on completion: UPDATE status = 'completed', write to Ledger
     -> on failure: classify failure, update retry_after or surface failure card
```

### 11.3 Cron Jobs: v1 Schedule

Fixed schedules defined in `jobs/scheduler.py` as `(hour, minute, weekday)` tuples. The scheduler is a single asyncio background task that computes the next firing across all entries, sleeps until that moment, enqueues the matching job to `jobs.db`, then recomputes. No external scheduler library, consistent with the "asyncio only" stance in Section 16.4. User-configurable via CLI or Web UI.

```
health_daily_meal_plan         -> daily 7:00 AM
university_canvas_check        -> daily 8:00 AM
job_internship_update          -> daily 9:00 AM
finance_expense_summary        -> daily 10:00 PM
university_weekly_summary      -> every Monday 8:00 AM
finance_weekly_budget_check    -> every Sunday 6:00 PM
task_context_cleanup           -> daily 3:00 AM (Task Context Object file cleanup)
```

### 11.4 Event-Driven Jobs

Triggered by signals rather than time. Event sources in v1 are limited (no native integrations) but the infrastructure is ready.

```
assignment deadline in 48 hours  -> trigger university agent warning
expense logged above threshold   -> trigger finance agent budget check
task marked complete             -> trigger north star progress check
```

Event-driven jobs enter the same queue as cron jobs with `type: event`, `scheduled_at: now`, `priority: 1`.

### 11.5 Async Jobs

Created mid-task by the failure handling flow (Queue for Later) or by agents that spawn background work.

```
Finance agent fails (rate limit)
-> user selects "Queue for Later"
-> job created: type=retry, retry_after=now + cooldown from Retry-After header
-> job processor picks it up after cooldown
-> Orchestrator reads agent_output from Ledger for completed agents
-> reconstructs Task Context Object
-> retries only the failed agent
-> if succeeds: Information card sent
-> if fails again: failure card surfaced
```

---

## 12. Storage Model

All storage is local SQLite and markdown files. Nothing proprietary, battle-tested concurrency via WAL mode.

### 12.1 File Layout

```
~/.north/
  ledger.db              <- all ledger entries (append-only)
  jobs.db                <- persistent job queue
  tools.db               <- tool confidence scores per agent
  inference_cache.json   <- last known OpenRouter model pool (fallback if refresh fails)
  secret.key             <- shared secret for notification callbacks and REST API auth
  tasks/
    task_{id}.db         <- one SQLite file per active task (Task Context Object)
                            cleaned up per Section 6.6 cleanup policy
  context/
    public.md
    private.md           <- local only, never synced, never leaves machine
    privacy_rules.md
    judgement_rules.md
    north_stars.md
```

### 12.2 Privacy Routing

Before any data is written anywhere, the privacy routing layer checks `privacy_rules.md` to determine where it goes. This is automatic and transparent.

```
content flagged as sensitive -> local files only (private.md, flagged ledger entries)
everything else              -> standard local storage
```

`private.md` never leaves the machine under any circumstances. This is a hard, permanent design constraint.

### 12.3 Future: Cloud and Semantic Search Layer

When context files grow large enough that they no longer fit in a context window and semantic search becomes necessary, the `ContextStore` swaps from `FileContextStore` to a `DBContextStore` backed by a vector database. One line change in the Orchestrator initialization. Nothing else changes.

The `search()` method raising `NotImplementedError` in `FileContextStore` marks exactly where this seam is. Any code that accidentally calls `search()` in v1 will fail loudly and immediately rather than silently returning empty results.

---

## 13. End-to-End Data Flow

Tracing a complete example. User says: "Help me prep for my first week at LinkedIn."

```
1. User double-taps Fn key and speaks the prompt.
   Whisper transcribes locally.
   Text sent to POST /orchestrator/task (with X-North-Secret header).
   Ledger write (async): source=mic, input="Help me prep for my first week at LinkedIn"

2. Classifier (high_volume pool):
   "internship prep" -> consequential
   Ledger write (async): source=system, action="classified: consequential"
   Proceed to north star check.

3. North Star Check (reasoning pool):
   Reads north_stars.md via ContextStore.
   "Crush LinkedIn internship" is a 1-year north star. Full alignment.
   Ledger write (async): source=system, action="north_star_check: aligned"
   Proceed to routing.

4. Routing Decision (reasoning pool):
   Reads agent registry and public.md.
   Decides: job agent + university agent (check for schedule conflicts).
   parallel_groups: [["job", "university"]], no dependencies.
   Task Context Object created: ~/.north/tasks/task_abc123.db
   Ledger write (async): source=system, action="routed", agents=["job","university"]

5. Job agent spins up (reasoning pool, high priority):
   Traverses tool graph: calendar_api [0.9], web_search [0.7], gmail_api [0.6]
   Loads those three tool definitions into context (sorted by confidence).
   Reads public.md: LinkedIn internship, distributed systems team, June 2nd start.
   Reads judgement_rules.md: prefers mornings for deep work.
   Produces first-week prep plan: onboarding checklist, team research, tool setup.
   Writes to task_abc123: job.prep_plan = { full structured JSON }
   Ledger write (async): source=agent, agent=job, agent_output={full JSON}, output="prep plan created"
   Inference Router logs (async): source=inference_router, model=claude-sonnet, cost_usd=0.0031

6. University agent spins up simultaneously (fast_cheap pool, medium priority):
   Reads public.md: SJSU schedule, current courses.
   Checks calendar for June conflicts. Finds none.
   Writes to task_abc123: university.schedule_conflicts = []
   Ledger write (async): source=agent, agent=university, agent_output={full JSON}, output="no conflicts found"

7. Orchestrator reads task_abc123. Both agents completed. No conflicts, no questions raised.
   Task is consequential -> routes to Approval Layer as Approval card.

8. Approval Layer sends macOS notification:
   "LinkedIn first week prep plan is ready."
   [Approve]  [View Detail]

9. User taps View Detail.
   Web UI on second monitor renders the full prep plan.
   User reads it and taps Approve in the Web UI.

10. Web UI calls POST /orchestrator/approval/respond (with X-North-Secret header).
    Orchestrator validates secret.
    Ledger write (async): source=approval, status=approved
    Extraction pipeline appends to judgement_rules.md:
      "Approves detailed onboarding checklists [confidence: 1/10]"
    task_abc123 status set to completed.
    Information card sent: "LinkedIn prep plan approved."

Total effort from user: one voice sentence, one tap.
```

---

## 14. Repository Structure

```
north/
  orchestrator/
    app.py              <- FastAPI app, lifespan (DB init, background tasks), Uvicorn entry point
    api_router.py       <- all REST endpoints (tasks, ledger, context, jobs, inference, agents)
    orchestrator.py     <- core orchestration logic (classify → north star → route → execute)
    classifier.py       <- trivial vs consequential classification
    north_star.py       <- north star alignment check
    router.py           <- agent routing and parallel execution planning
    task_context.py     <- Task Context Object management (SQLite per task)
    failure_handler.py  <- failure classification and retry logic
    stream.py           <- SSE event stream for Web UI real-time updates
    models.py           <- request/response Pydantic models
    exceptions.py

  agents/
    base.py             <- Agent (ABC)
    llm_agent.py        <- LLM-backed agent base class
    registry.py         <- agent discovery and registration
    models.py           <- AgentResult, AgentStatus
    exceptions.py
    health/
      agent.py
      config.yaml
      tools.yaml
      prompts/
        system.md
      README.md
    university/
    job/
    finance/

  context/
    __init__.py
    base.py             <- ContextStore (ABC)
    models.py           <- ContextDocument (enum of the five valid document names)
    exceptions.py       <- ContextError, ContextReadError, ContextWriteError
    file_store.py       <- FileContextStore (v1 concrete)
    extraction.py       <- extraction pipeline (Ledger → context docs, background job)
    injection.py        <- manual context injection handler (file, text, URL)

  ledger/
    __init__.py
    base.py             <- LedgerWriter (ABC), LedgerFilters
    models.py           <- LedgerEntry, LedgerSource, LedgerStatus
    exceptions.py       <- LedgerError, LedgerWriteError, LedgerReadError
    sqlite_writer.py    <- SQLiteLedgerWriter (concrete)

  inference/
    __init__.py
    base.py             <- InferenceRouter (ABC), CompletionRequest/Response
    openrouter.py       <- OpenRouterInferenceRouter (dynamic pools, auto-fallback)
    fallback_pools.py   <- hardcoded minimal pools if OpenRouter is unreachable on startup
    models.py           <- PoolPriority, ModelPool
    exceptions.py

  approval/
    __init__.py
    base.py             <- Notifier (ABC)
    macos.py            <- MacOSNotifier / AlerterNotifier (alerter subprocess)
    terminal.py         <- TerminalNotifier (fallback for dev/test)
    callback_server.py  <- local server on port 8001 receiving notification callbacks
    models.py           <- Card, CardType, ApprovalDecision
    store.py            <- module-level approval_store singleton (Web UI visibility)
    judgement_filter.py <- pre-screens cards against judgement_rules.md before notifying
    exceptions.py       <- ApprovalError, NotificationError

  jobs/
    __init__.py
    base.py             <- JobProcessor (ABC)
    sqlite_processor.py <- SQLiteJobProcessor (polls jobs.db every N seconds)
    scheduler.py        <- cron job definitions and scheduling logic
    models.py           <- Job, JobStatus, JobType
    exceptions.py

  tools/
    __init__.py
    base.py             <- Tool (ABC)
    registry.py         <- tool graph definition and edge traversal
    confidence.py       <- confidence score read/write against tools.db
    models.py           <- ToolEdge, ToolResult
    exceptions.py
    implementations/
      web_search.py
      calendar_api.py
      gmail_api.py
      canvas_api.py
      nutrition_api.py
      market_data_api.py
      linkedin_api.py
      fitness_tracker.py
      expense_tracker.py

  web/
    routes.py           <- Jinja2 routes for all /ui/* pages (configure() singleton)
    templates/
      base.html         <- layout, nav, auth meta-redirect
      dashboard.html    <- live activity feed + task input
      approvals.html    <- approval surface (cards, respond buttons)
      context_index.html
      context_doc.html  <- view/edit a context document
      agents.html
      jobs.html
      inference.html
      ledger.html
    static/
      css/main.css
      js/main.js

  cli/
    main.py             <- CLI entry point (Typer), thin wrapper over Orchestrator REST API

  config/
    settings.py         <- Settings (pydantic-settings), all NORTH_* env vars
    dependencies.py     <- build_production_dependencies(), shared dependency wiring

  utils/
    db.py               <- SQLite connection helpers (WAL mode, row_factory)
    ids.py              <- task/job/card ID generation
    prompts.py          <- prompt template loading
    security.py         <- secret key generation, verification, cookie + header auth
    time.py             <- datetime helpers

  prompts/
    classifier.md       <- system prompt for the Orchestrator classifier
    north_star.md       <- system prompt for the north star check
    router.md           <- system prompt for the routing decision

  tests/
    integration/
    unit/
      context/
      jobs/
      ledger/
      tools/
      utils/

  docs/
    CODING_STYLE.md

  exceptions.py         <- top-level NorthError base exception
  pyproject.toml
  uv.lock
  .env.example
  README.md
```

---

## 15. Open Questions

The following are deliberately deferred. No coding agent should make decisions on these without explicit spec updates.

**Context Storage Migration**
When do context files become too large for LLM context windows and semantic search becomes necessary? No action needed until the system is running. The `ContextStore` interface and the `NotImplementedError` on `search()` mark exactly where the implementation gap is.

**Native Integrations**
Canvas, Gmail, Google Calendar APIs. Deferred for v1. The job processor and event-driven infrastructure are ready to receive polling jobs or webhook signals when integrations are added.

**Mobile App**
Approval surface on mobile. Post v1. macOS notifications on the primary machine are sufficient for v1. *Note:* the Section 16.8 switch to HTMX + server-rendered templates means a mobile-friendly approval surface is now reachable with template-level changes rather than a separate codebase. The deferral stands; the cost picture changed.

**Proactive Orchestration**
The Monday morning briefing: Orchestrator waking itself on a schedule to summarize the week ahead without any user prompt. The cron job infrastructure is ready. The specific content format is not yet defined.

**Multi-device Context Sync**
How `judgement_rules.md` and `north_stars.md` stay consistent across multiple machines. Deferred until the system is stable on a single machine.

**Confidence Decay**
Judgement rules learned a long time ago may become stale as preferences change. A decay model (reducing confidence on rules not confirmed in the last N days) is not yet designed.

**Error Recovery and Context Correction**
Manual override of specific judgement rules, rollback of bad context deltas, reprocessing Ledger entries with a corrected extraction prompt. Mechanism not yet designed.

**Persona Layer**
Loading mental models of specific thinkers as advisory lenses on Orchestrator decisions. Deliberately post v1.

**Offline Transcription Fallback**
Voice input (Section 3.1) depends on OpenRouter being reachable. north has no fallback when the network is down or OpenRouter is degraded. A local Whisper variant (e.g. `mlx-whisper`) behind the same Inference Router interface would close this gap, but the threshold for when it is worth shipping — and the policy for switching modes — is not defined.

---

## 16. Tech Stack

Every technology choice here is a firm decision, not a suggestion. Coding agents must not substitute alternatives without an explicit spec update.

### 16.1 Language and Runtime

**Python 3.12+**
The entire backend (Orchestrator, agents, job processor, inference router, ledger, approval layer) is written in Python. No other backend language is used.

**Package manager: uv**
All dependencies are managed with `uv`. Do not use pip, poetry, or conda. `uv` is significantly faster and produces a locked `uv.lock` file for reproducible installs.

```bash
uv sync          # install all dependencies
uv add {package} # add a new dependency
uv run {command} # run a command in the project environment
```

### 16.2 Server

**FastAPI + Uvicorn**
The Orchestrator REST API runs on FastAPI served by Uvicorn on `localhost:8000`.

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
pydantic>=2.0.0
sse-starlette>=2.0.0     <- SSE streaming for Web UI activity feed
```

FastAPI handles:
- All REST endpoints defined in Section 6.8
- SSE stream via `sse-starlette` (`GET /orchestrator/stream`)
- Request and response validation via Pydantic models
- Automatic OpenAPI docs at `localhost:8000/docs` (available in development)

Uvicorn handles:
- ASGI server
- Single process, single worker (localhost only, no load balancing needed)
- Async event loop for parallel agent coroutines

The notification callback server (Section 9.1) runs as a second FastAPI app on `localhost:8001` within the same Uvicorn process using `Mount`.

### 16.3 Database

**SQLite via the standard library**
All SQLite access uses Python's built-in `sqlite3` module directly. No ORM. Raw SQL only.

WAL mode is enabled on every database at connection time:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
```

Four SQLite databases:
```
~/.north/ledger.db       <- ledger entries
~/.north/jobs.db         <- job queue
~/.north/tools.db        <- tool confidence scores
~/.north/tasks/          <- one .db file per task (Task Context Object)
```

### 16.4 Async

**asyncio (standard library)**
All concurrency uses Python's built-in `asyncio`. No threading, no multiprocessing, no Celery, no Redis. Parallel agent execution uses `asyncio.gather()`. The job processor runs as an asyncio background task inside the same event loop as the FastAPI server.

```python
# Parallel agent execution
results = await asyncio.gather(
    run_agent("job", task_id),
    run_agent("university", task_id)
)
```

### 16.5 LLM and Inference

**OpenRouter via httpx**
All LLM API calls go through OpenRouter. HTTP calls use `httpx` with async support.

```
httpx>=0.27.0
```

No LLM framework (LangChain, LlamaIndex, etc.). Direct API calls only. The Inference Router in `inference/router.py` manages everything (pool building, model selection, fallback, cost logging).

### 16.6 Voice Transcription

**OpenRouter Audio API**
Transcription runs through OpenRouter's `POST /api/v1/audio/transcriptions` endpoint (announced May 2026) using the same `NORTH_OPENROUTER_API_KEY` north already uses for LLM inference. One provider, one key, one billing surface.

The Inference Router (Section 8) owns the transcription call exactly as it owns LLM calls. The same fallback and cost-logging path applies: every transcription writes a Ledger entry with `source: inference_router`.

Default transcription model: `groq/whisper-large-v3` (sub-second latency). Alternatives selectable via the Inference Router without code change:

- `openai/whisper-1` — lowest cost
- `openai/gpt-4o-transcribe` — highest accuracy on technical speech
- `google/chirp-3` — strong on accented English

No additional Python dependency. The existing `httpx` client handles the request.

### 16.7 Context Injection: File Parsing

Document parsing for manual context injection (`north context add --file`):

```
pypdf>=4.0.0          <- PDF text extraction
python-docx>=1.0.0    <- Word document extraction
httpx>=0.27.0         <- URL fetching (already listed above)
beautifulsoup4>=4.12  <- HTML parsing for URL ingestion
```

### 16.8 Frontend

**HTMX + Jinja2**
The Web UI is server-rendered Jinja2 templates with HTMX for interactivity. No npm, no build step, no separate frontend process. The Orchestrator's existing FastAPI app serves templates and static assets directly at `localhost:8000/ui` via `fastapi.templating.Jinja2Templates` and a `StaticFiles` mount.

```
jinja2>=3.1.0
htmx              <- vendored as a single .js file in web/static/
```

SSE wiring (uses Section 6.8's `GET /orchestrator/stream`) is HTMX's built-in SSE extension:

```html
<div hx-ext="sse"
     sse-connect="/orchestrator/stream"
     sse-swap="activity-event">
</div>
```

Approval cards are plain `<form>` elements with `hx-post` to `/orchestrator/approval/respond`. No client-side state library, no hydration step.

The shared secret is set as an HttpOnly session cookie on first load via `GET /auth/token` (localhost-only endpoint). It is never embedded in rendered HTML or accessible to JavaScript.

Styling is a single hand-rolled `web/static/style.css` for v1. A classless CSS framework (e.g. Pico.css) can be added later as a separate decision if richer styling is needed.

### 16.9 CLI

**Typer**
The CLI is built with Typer, which is built on top of Click and integrates naturally with FastAPI's Pydantic models.

```
typer>=0.12.0
rich>=13.0.0     <- terminal formatting for ledger output, agent status, cost tables
```

Every CLI command is a thin wrapper over the Orchestrator REST API via `httpx`. The CLI reads `~/.north/secret.key` directly from the filesystem to populate the `X-North-Secret` header.

### 16.10 macOS Notifications

**alerter**
macOS native notifications with action buttons via `alerter`, called as a subprocess from Python. `alerter` is the actively maintained Swift fork of `terminal-notifier` and is the only command-line tool that still supports notification action buttons on current macOS releases (`terminal-notifier` itself dropped action button support).

```bash
brew install vjeantet/tap/alerter  # one-time setup
```

```python
import subprocess

subprocess.run([
    "alerter",
    "-title", "north",
    "-message", "LinkedIn prep plan is ready.",
    "-actions", "Approve,Reject,View Detail",
    "-reply", f"http://localhost:8001/callback?secret={secret}&action=ACTIONVALUE"
])
```

The `Notifier` ABC (see docs/CODING_STYLE.md Section 6.1) hides this choice from the rest of the system. Swapping `alerter` for a pyobjc-native UserNotifications binding later is a one-line dependency change.

### 16.11 Environment and Configuration

**Environment variables** for secrets and configuration that cannot be in the repo:

```bash
NORTH_OPENROUTER_API_KEY=sk-or-...   # required, set once
NORTH_NORTH_HOME=~/.north            # optional, default ~/.north
NORTH_NORTH_ENV=development          # development | production | test
```

**`~/.north/secret.key`** is generated automatically on first `north start` if it does not exist. It is never committed to the repository.

**`pyproject.toml`** is the single source of truth for dependencies, scripts, and tool configuration:

```toml
[project]
name = "north"
version = "1.0.0"
requires-python = ">=3.12"

[project.scripts]
north = "cli.main:app"         # makes `north` available as a CLI command after install

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "httpx>=0.27.0",           # for TestClient in FastAPI tests
]
```

### 16.12 Getting Started

north runs on macOS (arm64 or x86_64). The Approval Layer (Section 9) uses macOS native notifications and is macOS-only by design.

#### Prerequisites

Install these once. They do not need to be repeated per project.

**`uv`** — Python package manager. If you do not have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**`alerter`** — macOS notifications with action buttons (Section 16.10):
```bash
brew install vjeantet/tap/alerter
```

Grant notification permission when macOS prompts on first run, or open **System Settings → Notifications → alerter** and enable it manually.

**Python 3.12+** — managed automatically by `uv`. No separate install needed.

#### Install from source

```bash
# 1. Clone
git clone https://github.com/your-username/north
cd north

# 2. Install north and all dependencies. Wires the `north` command onto your PATH.
uv tool install .

# 3. Configure your API key
cp .env.example .env
# Open .env and set:
#   NORTH_OPENROUTER_API_KEY=sk-or-your-key-here
# Get a key at https://openrouter.ai/keys

# 4. Start
north start
```

On first run `north start` creates `~/.north/`, generates `secret.key`, and initialises all SQLite databases. When it is ready you will see:

```
★ north  Orchestrator → http://127.0.0.1:8000
         Web UI       → http://127.0.0.1:8000/ui/
         API docs     → http://127.0.0.1:8000/docs
         Home         → ~/.north
```

Open `http://127.0.0.1:8000/ui/` in a browser. The first visit sets the auth cookie and lands you on the dashboard. Submit a task to confirm everything is working.

#### Verify from the CLI

In a second terminal while north is running:

```bash
north tasks          # list active tasks
north ledger         # recent ledger entries
north inference models  # current model pool state
```

#### Update

```bash
uv tool upgrade north
```

#### For developers (editable install with dev tools)

```bash
git clone https://github.com/your-username/north
cd north

# Install all dependencies including dev group (pytest, etc.)
uv sync

# Configure
cp .env.example .env
# Edit .env: NORTH_OPENROUTER_API_KEY=sk-or-...

# Run (auto-reload on file changes)
uv run north start --reload

# Run tests
uv run pytest tests/unit/

# Lint and format
uv run ruff check .
uv run ruff format .
```

#### Future: one-liner installer (not yet implemented)

The intended end-state for non-developer users is:

```bash
curl -LsSf https://north.dev/install.sh | sh
north start
```

The installer would handle all prerequisites and the `uv tool install` step automatically. The `north.dev` domain and PyPI package name are placeholders pending availability checks. The manual steps above are the working path in the meantime.

### 16.13 Complete Dependency List

```toml
[project.dependencies]
# Server
fastapi = ">=0.110.0"
uvicorn = ">=0.28.0"
pydantic = ">=2.0.0"
pydantic-settings = ">=2.2.0"   # Settings class, NORTH_* env var binding
jinja2 = ">=3.1.0"              # server-rendered templates for the Web UI (Section 16.8)
python-multipart = ">=0.0.29"   # FastAPI file upload (UploadFile)

# HTTP client
httpx = ">=0.27.0"

# CLI
typer = ">=0.9.0"

# Config
pyyaml = ">=6.0"                # agent config.yaml / tools.yaml parsing

# Document parsing
pypdf = ">=4.0.0"
python-docx = ">=1.0.0"
beautifulsoup4 = ">=4.12.0"

# Voice input
sounddevice = ">=0.4.6"         # push-to-talk audio capture
pynput = ">=1.7.6"              # keyboard listener for push-to-talk hotkey
numpy = ">=1.26.0"              # sounddevice returns numpy arrays
```

Scheduling has no external dependency: cron entries are `(hour, minute, weekday)` tuples and `jobs/scheduler.py` is a single asyncio background task (see Section 11.3).

---

*You set the destination. north handles the navigation. You live the journey.*
