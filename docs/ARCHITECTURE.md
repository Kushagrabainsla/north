# north: System Specification
### Personal Life Operating System
> Version 1.3 · May 2026

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

> **See also:** [docs/TECHNICAL_FEATURES.md](TECHNICAL_FEATURES.md) — deep-dives on the twelve most interesting engineering decisions (ReAct loop, dynamic pool tiering, EMA scoring, context compaction, SLSA attestation, etc.)

> **What changed in 1.3:** asyncio.Event-based approval waiting (§9), JSON mode for all structured-output callers (§8.2), native function calling + token streaming in the ReAct loop (§7.6, §8.7), EMA confidence scoring (§7.5), semantic context search via OpenRouter embeddings (§5.7), episodic memory layer (§5.8), webhook event ingestion (§4.3, §6.8, §11.5), and two new SQLite stores (`embeddings.db`, `episodic.db`, §12.1).
>
> **What changed since 1.3:** curl installer (`scripts/install.sh`), GHCR image publishing via GitHub Actions, bundled `cli/docker-compose.yml` so install works without cloning the repo, `$HOME` workspace mount in Docker, workspace auto-detection from CWD in `north task` / `north chat`, `~/.north/.env` as the canonical config location, and fixed `north tasks` returning stale historical entries (§6).
>
> **What changed (modular refactor + inference hardening):** extracted `agents/constants.py`, `agents/schemas.py`, `agents/context_compaction.py`, `inference/constants.py`, and `orchestrator/constants.py` — no module-level code lives inline in its parent module anymore; `duration_ms` and `error_type` columns added to `LedgerEntry` with idempotent SQLite migration (§4.2); `classify_error()` in `failure_handler.py` maps any exception to a stable string tag written to `error_type` (§6.7); CI split into parallel `lint` and `test` jobs with `astral-sh/setup-uv` caching and per-job `timeout-minutes`; Docker workflow updated to multi-platform (`linux/amd64`, `linux/arm64`), GHA layer cache, BuildKit SBOM + provenance, and SLSA attestation via `actions/attest@v4`.
>
> **What changed (1.3.3 — multi-provider inference):** replaced `OpenRouterInferenceRouter` with `ModelDispatcher` (multi-provider), added `GroqRouter` and `GeminiRouter`, introduced `ModelCapability`/`ModelInfo`/`Provider` protocol, per-model EMA confidence tracking (`inference/dispatcher.py`), `ContextTooLargeError` caught and compacted in `AgenticLLMAgent`.

---

## 1. Vision

Most of what we call work in daily life is not real thinking. It is coordination overhead. Planning a week, tracking finances, managing academic deadlines, preparing for an internship: none of this requires you specifically. It just requires context about you.

north is a personal AI operating system that runs continuously in the background. You give it a north star (what you want to achieve, who you want to become) and it handles the operational work across every domain of your life. You review, approve, and enjoy the output. The cognitive load of coordination disappears.

**Core principle:** You should spend your time thinking, deciding, and experiencing, not managing. north manages so you do not have to.

---

## 2. System Overview

north is built from six distinct layers — Perception, Orchestrator, Agent, Approval, plus the shared Ledger and Context layers. Each has one clear job. Together they form a pipeline from raw input to real-world execution on your behalf.

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

The trade-off is explicit: audio leaves the machine in exchange for sub-second transcription latency. The `Notifier`-style pattern (`docs/CODING_STYLE.md` Section 6.1) keeps a future local fallback (e.g. `mlx-whisper`) cheap to add if local-first ever becomes a requirement again.

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
  duration_ms     INTEGER,           -- wall-clock duration of the event in milliseconds
  error_type      TEXT,              -- stable tag from classify_error(): rate_limit | context_overflow | timeout | network | parse_error | config_error | logic_error
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
webhook             task triggered by an external service via POST /orchestrator/webhooks/{source}
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
    async def read(self, document: ContextDocument) -> str:
        """Read a full context document. Returns '' if not yet written."""
        ...

    @abstractmethod
    async def write(self, document: ContextDocument, content: str) -> None:
        """Overwrite a context document entirely."""
        ...

    @abstractmethod
    async def append(self, document: ContextDocument, delta: str) -> None:
        """Append a delta (one line) to a context document."""
        ...

    async def search(self, query: str, max_results: int = 5) -> str:
        """Semantic search across all five context documents.

        Returns the top-k most relevant paragraphs, each labelled with its
        source document.  Uses cosine similarity when an EmbeddingIndex is
        attached (v1.3+); falls back to keyword overlap scoring when not.
        Returns '' when nothing is relevant.
        """
        ...
```

`FileContextStore` is the concrete v1 implementation.  It optionally accepts an `EmbeddingIndex` at construction time (see Section 5.7).  When present, `write()` and `append()` schedule a background re-indexing task; `search()` uses cosine similarity.  When absent, `search()` uses paragraph-level keyword scoring.  The rest of the system is unaware of which mode is active.

### 5.3 The Five Documents

#### public.md
General facts about the user freely available to all agents. Goals, preferences, schedule patterns, dietary habits, risk appetite, professional background. Updated continuously by the extraction pipeline.

#### private.md
Sensitive information agents cannot read automatically. Specific account numbers, medical details, relationship dynamics. **Stored locally only. Never leaves the machine.**

`private.md` is excluded from every agent's context by default — only agents with an explicit `can_read: private.md` entry in `privacy_rules.md` can access it, and this is enforced at context-load time in `Agent._load_context()`.

The dynamic flow described below (agent raises a runtime request → user approves → agent gets temporary access) is **not yet implemented**. Until it is, agents that need specific private facts should have them injected via `north context add --text "..."`, which routes through the extraction pipeline into the appropriate document.

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

### 5.7 Semantic Search and Embedding Index

`FileContextStore.search()` uses an `EmbeddingIndex` backed by `~/.north/embeddings.db` when one is wired in at startup.

**How it works:**

1. **Indexing** — every `write()` or `append()` call schedules a background `asyncio.create_task` that chunks the updated document into paragraphs, calls `InferenceRouter.embed()` in a batch, and stores `(doc, chunk_idx, chunk_text, embedding_vector)` rows in `embeddings.db`.  Indexing never blocks the write path.

2. **Retrieval** — `search(query)` embeds the query string (one API call), computes cosine similarity against every stored paragraph vector using numpy, and returns the top-k `[Source Document]\n<paragraph>` blocks as a single string.

3. **Fallback** — if the `EmbeddingIndex` is absent or the embed call fails, `search()` falls back to paragraph-level keyword overlap scoring (already implemented in v1.2).  Agents that call `search()` always get a result.

**Embedding model:** `openai/text-embedding-3-small` via OpenRouter's `POST /api/v1/embeddings` endpoint, same API key as inference.  Embedding calls are not tracked by the `CostTracker` — they are small enough that the noise is acceptable.

**Scope:** the embedding index covers the five context documents only.  It does not index the Ledger or the job queue.  Episodic memories (Section 5.8) have their own separate embedding store.

### 5.8 Episodic Memory

The episodic memory layer gives north a growing record of what it has actually done — not just facts about you, but memories of specific past tasks.

**What is stored:** after every completed task, the Orchestrator writes a summary of the form `Task: <prompt truncated to 120 chars>\nResult: <agent output truncated to 400 chars>` to `~/.north/episodic.db` together with an embedding of that summary.

**Retrieval:** before executing each agent run, `Agent._load_context()` queries the episodic store for the top-3 semantically similar past episodes (embedding cosine similarity, keyword fallback).  Any results are injected into the agent's context block as a `## Relevant past context` section containing bulleted summaries.

```
## Relevant past context
- Task: Search for internship applications due in June
  Result: Found 4 open applications: Stripe, Databricks, Cloudflare, Figma...
- Task: Draft follow-up email to Stripe recruiter
  Result: Email drafted and queued for approval...
```

This makes north progressively more context-aware about your patterns without the agent needing to re-search past ledger entries itself.

**Storage:** `~/.north/episodic.db`, schema:

```sql
CREATE TABLE episodes (
  id        TEXT PRIMARY KEY,
  task_id   TEXT,
  domain    TEXT NOT NULL,
  summary   TEXT NOT NULL,
  embedding TEXT,           -- JSON float array, null if embed call failed at write time
  timestamp TEXT NOT NULL
)
```

**Scope:** episodic search is over this table only.  The five context documents and the episodic store are complementary: context documents hold durable facts about you; the episodic store holds memories of specific past interactions.

---

## 6. The Orchestrator

The Orchestrator is the brain of north. It sits between the Perception Layer and the Agent Layer. Its job: receive intent, read context, check alignment, decompose into work, coordinate parallel execution, manage shared state, and handle failure.

### 6.1 Request Flow

Every input goes through four stages in order:

```
Input arrives directly from Perception Layer (voice or text)
       |
       v
Stages 1+3 combined: ExecutionPlanner.plan_all() (single LLM call, reasoning pool)
  -> classifies intent (trivial or consequential?) AND builds execution plan
  -> trivial:       skip Stage 2, proceed directly to Stage 4
  -> consequential: proceed to Stage 2
  -> Ledger write: source=system, action="classified as [trivial|consequential]"
  -> Ledger write: source=system, action="routed", agents=[...]

  Note: Stages 1 and 3 are merged into one LLM call in ExecutionPlanner.plan_all()
  to save cost and latency.  The IntentClassification and ExecutionPlan are returned
  together.  A separate IntentClassifier class no longer exists.
       |
       v
Stage 2: North Star Check (reasoning pool) — consequential tasks only
  -> reads north_stars.md via ContextStore
  -> aligns:    proceed to Stage 4
  -> conflicts: surface tension card to user, await decision before continuing
  -> Ledger write: source=system, action="north_star_check: [aligned|conflict]"
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

`classify_error(exc)` in `failure_handler.py` maps any exception to a stable string tag before any retry or notification logic runs. The tag is written to `LedgerEntry.error_type` so failure patterns are queryable from the Ledger.

```python
classify_error(RateLimitError())       # -> "rate_limit"
classify_error(asyncio.TimeoutError()) # -> "timeout"
classify_error(httpx.RequestError())   # -> "network"
classify_error(json.JSONDecodeError()) # -> "parse_error"
```

When an agent fails, the Orchestrator uses the classified error type to determine the appropriate response:

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

GET    /orchestrator/stream/{task_id}    -> SSE stream for real-time task progress (tokens,
                                            tool calls, approval cards, completion)

POST   /orchestrator/approval/respond    -> receive approval decision from callback server

POST   /orchestrator/transcribe          -> transcribe raw audio bytes via OpenRouter Whisper
                                            body: raw WAV/MP3 bytes
                                            returns: {text, model_used, cost_usd}

GET    /orchestrator/settings            -> read current user settings (strategy mode, etc.)
POST   /orchestrator/settings            -> update user settings

POST   /orchestrator/webhooks/{source}   -> receive an external event and submit it as a task
                                            auth: X-Webhook-Secret header (same shared secret)
                                            body: {prompt, context?}
                                            source: any string identifying the origin
                                                    (e.g. gmail, github, calendar, finance)
                                            returns: {task_id, status, source}
```

---

## 7. Agent Layer

Agents are domain specialists. Each knows one domain and operates only within it. They do not talk to each other directly. All communication goes through the Task Context Object managed by the Orchestrator.

### 7.1 Agent Set

The agent set is discovered from the `/agents` folder at startup (§7.2), so it evolves
without code changes. The current set:

| Agent | Domain | Responsibilities |
|-------|--------|-----------------|
| Architect | Engineering | Designs implementation plans, decomposes work — reasoning pool |
| Coder | Engineering | Code generation, debugging, refactoring, edits — reasoning pool |
| Tester | QA | Runs the suite, verifies changes, reports failures — fast_cheap pool |
| Researcher | Research | Open-ended investigation, gathering and synthesising sources |
| General | General purpose | Open-ended chat, planning, questions that don't map to a domain agent |
| Home | Smart home | Controls local devices (e.g. Kasa bulbs) behind approval gates |
| News Briefing | News | Assembles briefings from web sources |
| Health | Health and wellness | Meal planning, workouts, dietary and fitness tracking |
| University | Academic | Coursework, deadlines, research papers, communications |
| Job | Career | Internship tasks, professional communications, interview prep |
| Finance | Personal finance | Budgeting, expense tracking, investment research |

Every agent subclasses `LLMAgent` (single call) or `AgenticLLMAgent` (ReAct loop with native
function calling); the engineering agents (Architect, Coder) run on the reasoning pool.

### 7.2 Folder Structure

Each agent is a self-contained folder dropped into `/agents`. The Orchestrator scans this directory on startup and hot-reloads when it detects new or modified folders.

```
/agents
  /coder
    agent.py              <- core logic (usually a thin AgenticLLMAgent/LLMAgent subclass)
    config.yaml           <- declaration: agent, domain, model pool, accepted keywords
    tools.yaml            <- the specialized tools this agent gets (universal tools are implicit)
    prompts/
      system.md           <- system prompt defining the agent's expertise
  /architect/  /tester/  /researcher/  /general/  /home/  /news_briefing/
  /health/  /job/  /finance/  /university/
```

**config.yaml example:**
```yaml
agent: coder
domain: engineering
model_pool: reasoning
accepts:                   # routing keywords matched against the prompt
  - "code"
  - "implement"
  - "fix"
  - "debug"
output_format: structured_json
version: 1.0.0
class_name: CoderAgent     # the Agent subclass in agent.py
```

**tools.yaml example** (a plain list of specialized tool names; universal tools are granted
to every agent automatically and are not listed):
```yaml
tools:
  - bash
  - shell
  - git
  - gh
  - patch_file
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
   tools.yaml      <- specialized tools (universal tools are implicit)
   prompts/
     system.md     <- LLM-generated starter prompt for legal domain
   README.md       <- agent overview (domain, pool, accepted keywords)
```

`north agent create` also adds the new domain to `prompts/planner.md` so the orchestrator can
route to it.

The generator uses a reasoning pool call to produce a reasonable starting `system.md` based on the declared domain and tasks. The agent has a running start, not a blank page.

### 7.4 Tool Graph

Tools are discovered from the filesystem, not assigned by a hand-written graph. `ToolRegistry`
(`tools/registry.py`) walks the tool package directories and registers every `Tool` subclass it
finds. The agent→tool mapping is then a two-tier graph:

- **Universal tools** (`tools/universal/`) are granted to *every* agent: `read_file`,
  `write_file`, `glob`, `list_dir`, `search_files`, `web_search`, `fetch_url`,
  `schedule_task`, `create_tool`, `create_agent`, `query_metrics`.
- **Specialized tools** are opt-in per agent. Each agent lists the ones it wants in its
  `tools.yaml`; the registry maps that into the graph at load time.

```
Specialized tool edges (from each agent's tools.yaml):

coder ───> bash, shell, git, gh, patch_file
tester ──> bash, shell, git, gh
home ────> kasa
researcher ─> (universal only)

Universal tools ── granted to every agent ──> read_file, write_file, glob, list_dir,
                                              search_files, web_search, fetch_url, …
```

`tools_for_agent(agent)` returns the universal set plus that agent's specialized set, sorted
by confidence score (§8). `update_graph(agent, names)` adjusts edges at runtime — e.g. when a
tool is hot-loaded mid-task by `create_tool`. An agent loads only its own tools into context,
so there is no token waste from irrelevant tool definitions.

**Context loading order:** when an agent spins up, it loads its tool definitions into context sorted by confidence score descending. Low confidence tools are only loaded if the specific task explicitly requires them. This keeps the agent's context window lean.

### 7.5 Confidence Scoring and Persistence

Every tool edge in the graph carries a confidence score from 0.0 to 1.0. Scores are updated after every tool use via an **exponential moving average (EMA)** with smoothing factor α = 0.10:

```python
outcome = 1.0 if was_helpful else 0.0
new_confidence = clamp(0.10 * outcome + 0.90 * current_confidence, 0.0, 1.0)
```

The EMA means recent behavior dominates: a tool that failed early but now succeeds reliably recovers its score in ~10 successful uses.  The old fixed-delta approach (`+0.05 / -0.03`) took ~27 successful uses to recover from a low score — far too slow for the system to adapt.  Default prior for unseen tool pairs: 0.50.  Reliable filesystem/shell tools are seeded at 0.80 on startup via `seed_defaults()`.

**Persistence:** confidence scores are stored in `~/.north/tools.db`, a dedicated SQLite database. This is separate from the Ledger (event log) and separate from the Task Context Object (per-task scratch space). `tools.db` is the authoritative source for current confidence state.

```sql
CREATE TABLE tool_confidence (
  agent                TEXT NOT NULL,
  tool                 TEXT NOT NULL,
  confidence           REAL NOT NULL DEFAULT 0.5,
  uses_total           INTEGER NOT NULL DEFAULT 0,
  uses_helpful         INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_updated         DATETIME NOT NULL,
  PRIMARY KEY (agent, tool)
)
```

`consecutive_failures` is used to scale the EMA alpha on repeated failures (α doubles per consecutive failure, capped at 0.5), so a tool that keeps failing loses confidence much faster than one that occasionally fails.

On Orchestrator startup, all confidence scores are loaded from `tools.db` into memory. Every tool use updates the in-memory score and writes the delta to `tools.db`. Every confidence update is also logged to the Ledger with `source: system`.

**New agent inheritance:** when a new agent declares `similar_to: health` in `config.yaml`, the Orchestrator copies confidence rows from the `health` agent in `tools.db` as the new agent's starting prior. Tools not present in the source agent start at `initial_confidence` from `tools.yaml`.

### 7.6 The AgenticLLMAgent ReAct Loop (Function Calling)

Domain agents that extend `AgenticLLMAgent` run a ReAct loop using the OpenAI-compatible **function calling API** instead of JSON-in-text prompting.  This is the default for all v1 domain agents.

**Why function calling instead of JSON-in-text:**

The previous approach required the model to produce a raw JSON string matching a hand-crafted schema, then parsing it with `json.loads()`.  This failed silently when models wrapped output in markdown fences, produced partial JSON, or hallucinated tool names.  Function calling offloads schema enforcement to the provider: the model receives typed function definitions and returns a structured `tool_calls` object.

**Loop:**

```
messages = [system_prompt, user_message_with_task_and_context]
tools    = [typed function defs from tool.schema() for each tool] + [request_approval]

loop (max 12 iterations):
  compact older tool results in messages to preserve context window
  response = complete_with_tools(messages, tools, token_callback)

  if response.type == "message":
    stream tokens to SSE; return final answer      # done

  if response.type == "tool_call":
    execute tool (or request_approval)
    record confidence via ConfidenceTracker
    emit tool_called + tool_result SSE events
    append assistant tool-call turn + tool result to messages
    continue
```

**Token streaming (Section 8.7):** when the model produces a text response (the final answer), individual tokens are forwarded to the caller via an async `token_callback`.  `AgenticLLMAgent` passes a callback that emits `token` SSE events, so the Web UI renders the response progressively as it arrives.

**Tool schemas:** every `Tool` subclass declares a `parameters_schema` class variable (JSON Schema object).  The base class `schema()` method wraps it in the OpenAI function definition format.  Tools without an explicit schema use `{type: object, additionalProperties: true}` as a safe default.

**`request_approval` tool:** a synthetic tool injected into every agent's tool list.  When called, it creates an Approval card, emits `approval_required` via SSE, and blocks (via asyncio.Event — Section 9.7) until the user responds.  The model's decision to call this tool is treated like any other tool call.

### 7.7 The If-Unsure-Ask Rule

Agents follow a clear decision hierarchy when they encounter ambiguity:

1. Check Context Layer and `judgement_rules.md` first. The answer is probably already there.
2. Make a reasonable default, proceed, and flag it clearly in the output for the user to override via the Approval card.
3. If the decision is consequential and no clear default exists: stop and raise a Question through `orchestrator.ask()`.

When an agent raises a question, it sets `status: awaiting_input` in its Task Context Object row. The Orchestrator surfaces a Question card. The user answers via notification buttons or the Web UI. The answer is written back to the Task Context Object, the agent resumes, and the answered question is appended to `judgement_rules.md` so it is never asked again.

---

## 8. Inference Router

The Inference Router selects the appropriate LLM for every inference call in the system. Fully dynamic: no hardcoded model names in application code, no static config file for model assignments. Model selection is driven by task priority and the active inference strategy.

### 8.1 Providers

Inference is served by a `ModelDispatcher` that fans out across multiple providers.  OpenRouter is always included for broad model coverage; direct providers (Groq, Gemini) are prepended when their API keys are present so they are preferred for their own models.  Each provider has its own `NORTH_*_API_KEY` environment variable; only `NORTH_OPENROUTER_API_KEY` is required.

| Provider | Key | Notes |
|---|---|---|
| OpenRouter | `NORTH_OPENROUTER_API_KEY` | Required. Broadest model catalogue; embeddings; fallback for all tiers. |
| Groq | `NORTH_GROQ_API_KEY` | Optional. Free-tier fast completions; Whisper transcription. |
| Gemini | `NORTH_GEMINI_API_KEY` | Optional. Free-tier completions; embeddings. |

All providers share the same `Provider` protocol (`inference/provider.py`) and are registered into a single `ModelDispatcher` at startup via `inference/factory.py:build_router()`.

### 8.2 Dynamic Model Pools

Each provider exposes a `refresh()` method that fetches its live model list from the provider's API.  `ModelDispatcher.refresh_pools()` calls every registered provider in sequence and rebuilds its internal registry.  Pools refresh every 6 hours via a background task and once at startup.

Models are assigned a continuous `base_quality` score (0–1) derived from their output price via `quality_from_cost()` in `inference/capability.py` — log-scale normalisation over the ~$0.000001–$0.015/token pricing range.  `current_pools()` then bins by threshold for the CLI display:

```
reasoning pool:    base_quality ≥ 0.70  (most capable; frontier models)
fast_cheap pool:   base_quality ≥ 0.40  (mid-tier)
high_volume pool:  base_quality < 0.40  (cheapest)
free_fallback:     cost_per_token == 0  (free models, any quality)
```

A model can appear in both `free_fallback` and a quality tier.  Actual ranking within each pool blends `base_quality` with a live per-model EMA success rate (`_effective_quality()`).  When a new model releases or pricing changes, it enters the correct tier automatically without any manual action.

**Pool refresh failure handling:** if a provider's refresh call fails, `ModelDispatcher.refresh_pools()` logs a warning and retains the previously-loaded model registry for that provider. The Orchestrator continues accepting tasks in all cases.

**Background refresh loop:** the pool refresh loop uses a loop-first pattern — the initial sleep is at the bottom of the loop, not the top — so it fires immediately on Orchestrator startup, then repeats every 6 hours. This guarantees that fresh model IDs are in place before the first real inference call, without a separate startup refresh step.

**Error-triggered refresh:** when any model in the fallback chain fails, `_maybe_refresh_pools_background()` in `orchestrator.py` schedules a background refresh subject to a 60-second cooldown (`POOL_REFRESH_COOLDOWN` in `orchestrator/constants.py`). A 404 from a retired model ID triggers a live pool update so the next attempt uses current IDs, without hammering the OpenRouter `/models` endpoint on every failure.

### 8.3 Inference Strategy

The active strategy controls how models are ordered in the fallback chain for every call. Set via natural language ("switch to eco mode") or `POST /orchestrator/settings`. Persisted to `~/.north/settings.json`. Default: **cruise**.

```
eco     Cheapest model first, climbs up price ladder only on failure.
        Minimises cost; quality may vary on hard tasks.

cruise  Role-aware ordering (default). Maps task priority to the appropriate
        tier, then falls through adjacent tiers on failure:
          HIGH   -> reasoning -> fast_cheap -> high_volume -> free
          MEDIUM -> fast_cheap -> high_volume -> reasoning -> free
          LOW    -> high_volume -> fast_cheap -> reasoning -> free

sport   Most capable model first, descends to cheaper only on failure.
        Maximises quality regardless of cost.
```

The current strategy is shown in the terminal prompt (`[eco] ❯`, `[cruise] ❯`, `[sport] ❯`) and as a badge in the Web UI command bar.

### 8.4 Priority Signals

Every `CompletionRequest` carries a `PoolPriority` that `cruise` strategy uses to pick a starting tier. Components use priority as a signal of task complexity, not as a hard model assignment.

```
orchestrator routing      -> MEDIUM
north star check          -> MEDIUM
finance agent             -> HIGH (consequential domain)
job agent                 -> HIGH (consequential domain)
university agent          -> MEDIUM
health agent              -> MEDIUM
extraction pipeline       -> LOW (background job)
classifier                -> LOW (simple binary classification)
```

### 8.5 Automatic Fallback Chain

Every `complete()` and `complete_with_tools()` call walks the ordered candidate list produced by `ModelDispatcher._candidates()` until one succeeds. Three exception classes handle failures in the chain:

- `ModelRateLimitedError` — raised on HTTP 429, 404 (retired model), and 503. Applies a 60-second cooldown to the `(model_id, provider)` pair and silently advances to the next candidate.
- `PaymentRequiredError` — raised on HTTP 402. Applies a 24-hour cooldown and advances.
- `InferenceError` — raised on HTTP 400 (unsupported parameters, bad model ID) and other provider errors. Records a failure in the EMA, logs at `WARNING`, and advances to the next candidate.

Any other exception (network failures, unexpected errors) records a failure in the EMA and re-raises immediately to the caller.

The chain is exhausted only when every candidate has been tried or cooled down. Only then is `AllModelsRateLimitedError` raised to the caller.

```
sport strategy, any priority:
  claude-opus -> gpt-4o -> claude-sonnet -> gpt-4o-mini -> claude-haiku
  -> gemini-flash -> ... -> meta-llama:free -> qwen3-8b:free
```

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

`CostTracker` (an `InferenceRouter` decorator) intercepts every `complete()` and `complete_with_tools()` call and accumulates `cost_usd` keyed by `task_id`.  The Orchestrator calls `cost_tracker.pop_task_cost(task_id)` after all agents complete and emits the total in the `task_completed` SSE event.

Cost summary available via CLI and Web UI:
```bash
north inference costs --period week
north inference costs --period month
north inference costs --agent finance
north inference models           # show current pool state
```

### 8.6 JSON Mode

All callers that expect a structured JSON response (classifier, north star checker, extraction pipeline, context injection router) pass `json_mode=True` in their `CompletionRequest`.  The router forwards this as `response_format: {type: json_object}` in the OpenRouter request body.

This eliminates the entire class of "Failed to parse classifier output as JSON" errors that arose when models wrapped responses in markdown fences or produced partial output.  The model is still instructed to produce JSON via its system prompt; `json_mode` is a belt-and-suspenders guarantee at the provider level.

**Rule:** only set `json_mode=True` when the system prompt explicitly instructs JSON output.

### 8.7 Function Calling and Token Streaming

`InferenceRouter` exposes two additional async methods beyond `complete()`:

```python
async def complete_with_tools(
    request: ToolCallRequest,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
) -> ToolCallResponse:
    """Function-calling completion.  Returns either a tool call or a final
    text message.  Text tokens are streamed to token_callback as they arrive."""

async def embed(request: EmbedRequest) -> EmbedResponse:
    """Embed a batch of texts and return one float vector per input."""
```

`complete_with_tools` uses OpenRouter's streaming endpoint (`stream: true`) internally so text tokens from the final answer are forwarded to `token_callback` in real time.  Tool call arguments are accumulated from streaming delta chunks and resolved when `finish_reason: tool_calls` is received.  `CostTracker` wraps this method and accumulates `response.cost_usd` per `task_id` exactly as it does for `complete()`.

`embed` calls `POST /api/v1/embeddings` with `openai/text-embedding-3-small`.  Used by `EmbeddingIndex` (§5.7) and `EpisodicStore` (§5.8).

### 8.8 Audio Transcription

The Inference Router also owns audio transcription via OpenRouter's `POST /api/v1/audio/transcriptions` endpoint (see Section 16.6). The same client, the same `NORTH_OPENROUTER_API_KEY`, the same fallback semantics, and the same Ledger logging (`source: inference_router`) apply. Transcription is a separate code path from chat-completion (different endpoint, different request shape) but shares all infrastructure.

Default transcription model: `groq/whisper-large-v3`. The Inference Router exposes a configurable override the same way it exposes LLM model selection.

---

## 9. Approval Layer

The Approval Layer is the primary interface between north and the user for consequential outputs. Users do not interact with agents directly. They interact with notifications and the Web UI.

### 9.1 Notifications and Security

The Approval Layer sends notifications with action buttons. The default `Notifier` implementation (`TerminalNotifier`) prints approval cards to stdout/logs and works on any platform. An optional `MacOSNotifier` (also exported as `AlerterNotifier`) uses the `alerter` subprocess for native macOS notification banners with action buttons — swap it in via `config/dependencies.py` if running on macOS and alerter is installed. A local callback server runs on `localhost:8001` and receives button taps.

**Security:** the notification callback server is secured with a shared secret generated at first startup and stored at `~/.north/secret.key`. Every notification payload embeds this secret in the callback URL or request body. Every callback request to `localhost:8001` must include the `X-North-Secret` header with the correct value. Requests without a valid secret are rejected with HTTP 403. This prevents any other local process from faking an approval action.

The same shared secret is required on all calls to the Orchestrator REST API (Section 6.8).

```
Agent completes work
-> Approval Layer creates card payload
-> Sends notification (TerminalNotifier: stdout | MacOSNotifier: native banner)
   (callback URL contains the shared secret)
-> User taps action button
-> POST to localhost:8001/callback with secret
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

### 9.7 Event-Based Approval Waiting

When a coroutine needs to wait for a user decision (north star conflict approval, mid-task `request_approval` tool call), it calls `approval_store.wait_for_decision(card_id, timeout=300.0)` rather than polling in a loop.

```python
# Before (polling — held the event loop busy for up to 1 s per tick):
for _ in range(300):
    await asyncio.sleep(1)
    card = approval_store.get(card_id)
    if card and card.status != "pending":
        break

# After (event-based — wakes exactly when the user clicks):
card = await approval_store.wait_for_decision(card_id, timeout=300.0)
```

`ApprovalStore` allocates an `asyncio.Event` for each card on `add()`.  `resolve()` calls `event.set()`.  `wait_for_decision()` awaits the event with a 300-second `asyncio.wait_for` timeout.  Under load with many concurrent pending approvals, zero CPU is consumed while waiting — each coroutine is simply suspended until its specific event fires.

---

## 10. Interface Model

north has two primary interfaces. Both talk to the same Orchestrator REST API. The CLI is direct access. The Web UI makes HTTP calls to the same endpoints.

### 10.1 Web UI: Second Monitor Dashboard

A local web UI served by the Orchestrator at `localhost:8000/ui`. Server-rendered Jinja2 templates with HTMX for interactivity. No separate frontend process and no build step. Intended to run permanently on a second monitor, giving continuous visibility into everything north is doing.

**Three panels:**

**Live Activity Feed**
Real-time stream of Orchestrator activity via SSE (`GET /orchestrator/stream/{task_id}`). Every agent action, tool call, Ledger write, and job execution appears as it happens. Includes `token` events — individual text tokens streamed from the model's final answer as they arrive, enabling progressive rendering of the agent's response as it generates.

SSE event types:
```
classifying          Stage 1 started
classified           Trivial/consequential decision + domain
north_star_checking  Stage 2 started
north_star_conflict  Conflict detected — approval card incoming
north_star_aligned   Check passed
routing              Stage 3 started
routed               Agents selected + parallel groups
executing            Stage 4 started
agent_started        One agent beginning its ReAct loop
tool_called          Agent called a tool (includes tool name + params)
tool_result          Tool completed (includes success flag)
token                One text token from the final answer (streaming)
approval_required    An approval card needs user action
approval_responded   User made a decision
agent_completed      Agent produced its final answer
task_synthesis       Multi-agent outputs merged into one response
task_completed       Full task done (includes cost_usd)
task_cancelled       Task cancelled (includes reason)
task_failed          Unrecoverable error
```

**Approval Surface**
Full card rendering for complex approvals and questions. Approve, reject, and answer questions directly here without opening a notification. All cards that have been sent as notifications are also mirrored here.

**Control Panel**
- Submit text prompts to the Orchestrator (with session conversation history)
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
# Lifecycle
north                                    # open TUI (auto-starts server if not running)
north start                              # start server + TUI (local uvicorn by default)
north start --reload                     # local with auto-reload on file changes
north start --no-chat                    # start server only, skip TUI
north start --docker                     # start via Docker Compose (server/headless deployments)
north stop                               # stop (kills the server process or docker compose down)

# Task management
north task "Plan my week"
north task "What assignments are due this week?"
north tasks                              # list active tasks
north task cancel {id}

# Voice input (push-to-talk)
north dictate                            # hold hotkey, speak, release to submit

# Context management
north context show public
north context show north_stars
north context edit judgement_rules       # opens in $EDITOR
north context add --file resume.pdf
north context add --text "I prefer mornings for deep work"
north context add --url "https://example.com/article"

# Agent management
north agents
north agent create
north agent run health --task "meal plan for today"

# Ledger
north ledger                             # recent entries
north ledger --task {id}
north ledger --agent finance
north ledger --source manual_injection

# Job queue
north jobs                               # list all jobs
north jobs --status pending
north job cancel {id}

# Inference
north inference costs --period week
north inference costs --agent finance
north inference models                   # current model pool state + active providers

# Metrics
north metrics                            # per-agent task counts, success rates, costs, p50/p95 durations

# Tools
north tools confidence --agent health

# Debug
north stream {task_id}                   # stream raw SSE events for a task
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

### 11.3 Cron Jobs: Built-in + User-Defined

The `CronScheduler` runs as a single asyncio background task. It combines two sources of entries and re-evaluates them every 60 seconds so newly added schedules take effect within a minute.

**Built-in schedules** (`jobs/scheduler.py` — `V1_CRON_ENTRIES`):
```
health_daily_meal_plan         -> daily 7:00 AM
university_canvas_check        -> daily 8:00 AM
job_internship_update          -> daily 9:00 AM
finance_expense_summary        -> daily 10:00 PM
university_weekly_summary      -> every Monday 8:00 AM
finance_weekly_budget_check    -> every Sunday 6:00 PM
task_context_cleanup           -> daily 3:00 AM
```

**User-defined schedules** — stored in the `user_cron_entries` table in `~/.north/jobs.db`. Created three ways:
1. Natural language: "remind me every Monday at 9am to review my goals"
   → agent calls `schedule_task` tool with `hour`, `minute`, `weekday` params
2. Web UI: `/ui/jobs` → Recurring Schedules → "+ Add" form
3. API: `POST /orchestrator/cron`

User entries are deleted with `DELETE /orchestrator/cron/{name}` or via the Web UI delete button.

### 11.4 One-Shot Scheduled Jobs

A job enqueued with a future `scheduled_at` will sit as `pending` until the job processor clock catches up. Created three ways:
1. Natural language: "remind me tomorrow at 5pm to call the doctor"
   → agent calls `schedule_task` tool with `run_at` param (ISO 8601 UTC)
2. Web UI: `/ui/jobs` → One-Shot & Queue → "+ Schedule" form
3. API: `POST /orchestrator/jobs` with `scheduled_at` field

### 11.5 Event-Driven Jobs

Triggered by signals rather than time.  v1 now supports two event sources:

**Webhook ingestion** — external services send `POST /orchestrator/webhooks/{source}` with an `X-Webhook-Secret` header and a JSON body containing a `prompt` and optional `context` field.  The Orchestrator submits this as a task with `source: webhook` and the prompt prefixed with `[webhook:{source}]` so the classifier can route it correctly.

```bash
# A Gmail push notification triggers the job agent:
curl -X POST http://localhost:8000/orchestrator/webhooks/gmail \
  -H "X-Webhook-Secret: $NORTH_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Recruiter from Stripe replied to your application", "context": "sender: recruiter@stripe.com"}'
```

**Internal threshold events** — agents can schedule immediate threshold-triggered jobs via the `schedule_task` tool with `run_at: now`.  Infrastructure examples:

```
expense logged above monthly threshold -> finance agent budget check
canvas deadline in 48 hours            -> university agent reminder
```

All event-triggered tasks enter the main pipeline (classify → north star → route → execute) identically to user-initiated prompts.  Ledger entries use `source: webhook` (external) or `source: cron` (internal threshold jobs).

### 11.6 Async Jobs

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
  jobs.db                <- persistent job queue + user cron entries
  tools.db               <- tool confidence scores per agent (EMA-updated)
  embeddings.db          <- paragraph embedding vectors for the five context documents
  episodic.db            <- per-task summaries with embeddings for episodic retrieval
  tool_index.db          <- per-tool embedding vectors for semantic tool selection
  facts.db               <- per-fact embedding vectors for semantic context retrieval
  settings.json          <- user settings (inference strategy, etc.)
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

### 12.3 Embedding Storage

`embeddings.db` and `episodic.db` are both append-dominant SQLite databases using WAL mode.  They have no retention policy: embedding rows are replaced wholesale when a context document is overwritten, and episodic rows accumulate indefinitely (bounded by the 500-row `ORDER BY timestamp DESC LIMIT 500` query in `EpisodicStore`).

When context files grow large enough that paragraph-level embedding search degrades (thousands of paragraphs, many documents), the `EmbeddingIndex` can be replaced with a proper vector database (e.g. sqlite-vec, ChromaDB) behind the same interface with no changes to callers.  The `ContextStore.search()` contract is stable; the backing store is an implementation detail.

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
   Loads its tools (universal set + specialized) sorted by confidence: web_search [0.9], read_file [0.7], schedule_task [0.6]
   Loads those tool definitions into context (sorted by confidence).
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
    constants.py        <- MAX_CONCURRENT_TASKS, NORTH_STAR_CONFIDENCE_THRESHOLD, POOL_REFRESH_COOLDOWN, STRATEGY_CMD_RE
    classifier.py       <- trivial vs consequential classification
    north_star.py       <- north star alignment check
    router.py           <- agent routing and parallel execution planning
    task_context.py     <- Task Context Object management (SQLite per task)
    failure_handler.py  <- classify_error() + failure classification and retry logic
    stream.py           <- SSE event stream for Web UI real-time updates
    models.py           <- request/response Pydantic models
    exceptions.py

  agents/
    base.py                 <- Agent (ABC)
    llm_agent.py            <- LLM-backed agent base class
    agentic_llm_agent.py    <- AgenticLLMAgent: ReAct loop with native function calling
    registry.py             <- agent discovery and registration
    constants.py            <- MAX_DELEGATION_DEPTH, ENGINEERING_AGENTS, MAX_TOOL_RESULT_CHARS
    schemas.py              <- DELEGATE_TASK_SCHEMA, REQUEST_APPROVAL_SCHEMA (JSON Schema dicts)
    context_compaction.py   <- compact_history(), compact_if_needed(), context_window_for()
    models.py               <- AgentResult, AgentStatus, AgentDependencies
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
    file_store.py       <- FileContextStore (v1 concrete, optional EmbeddingIndex)
    embedding_index.py  <- EmbeddingIndex: SQLite paragraph vectors + cosine search
    episodic.py         <- EpisodicStore: per-task summaries + semantic retrieval
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
    base.py             <- InferenceRouter (ABC): complete, complete_with_tools, embed, transcribe
    dispatcher.py       <- ModelDispatcher: multi-provider router with per-model cooldowns and EMA
    factory.py          <- build_router(): assembles ModelDispatcher from available provider keys
    capability.py       <- ModelCapability, ModelInfo, quality_from_cost
    provider.py         <- Provider (Protocol): contract each inference provider must satisfy
    cost_tracker.py     <- CostTracker: InferenceRouter decorator, accumulates cost per task_id
    constants.py        <- base URLs, timeout, quality normalisation constants
    models.py           <- PoolPriority, ModelPool, ToolCallRequest/Response, EmbedRequest/Response
    exceptions.py       <- AllModelsRateLimitedError, ContextTooLargeError, PoolRefreshError, …
    providers/
      openai_compat.py  <- OpenAICompatibleProvider: shared HTTP base for OpenAI-format APIs
      openrouter.py     <- OpenRouterRouter: dynamic catalogue, embeddings, transcription
      groq.py           <- GroqRouter: free-tier completions and Whisper transcription
      gemini.py         <- GeminiRouter: free-tier completions and embeddings

  approval/
    __init__.py
    base.py             <- Notifier (ABC)
    macos.py            <- MacOSNotifier / AlerterNotifier (alerter subprocess)
    terminal.py         <- TerminalNotifier (fallback for dev/test)
    callback_server.py  <- local server on port 8001 receiving notification callbacks
    models.py           <- Card, CardType, ApprovalDecision
    store.py            <- ApprovalStore: asyncio.Event per card, wait_for_decision()
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
    base.py             <- Tool, AuthenticatedTool, CacheableTool (ABCs)
    registry.py         <- ToolRegistry: filesystem discovery + dynamic agent→tool graph
    tool_index.py       <- tool metadata index
    confidence.py       <- confidence score read/write against tools.db
    models.py           <- ToolInput, ToolOutput, ConfidenceScore
    exceptions.py
    universal/          <- granted to every agent (read_file, write_file, glob, list_dir,
                           search_files, web_search, fetch_url, schedule_task,
                           create_tool, create_agent, query_metrics)
    specialized/        <- opt-in per agent (bash, shell, git, gh, patch_file, kasa)
    semantic/           <- code intelligence (search_symbols, find_references)
    analysis/           <- static analysis (check_types)

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
  Dockerfile
  docker-compose.yml
  .dockerignore
  .env.example
  README.md
```

---

## 15. Open Questions

The following are deliberately deferred. No coding agent should make decisions on these without explicit spec updates.

**Native Integrations**
Canvas, Gmail, Google Calendar APIs. Deferred for v1. The job processor and webhook infrastructure (`POST /orchestrator/webhooks/{source}`) are ready to receive events when real integrations are added.

**Mobile App**
Approval surface on mobile. Post v1. The HTMX + server-rendered template stack means a mobile-friendly surface is reachable with template-level changes rather than a separate codebase. The deferral stands; the cost picture changed.

**Proactive Orchestration**
The Monday morning briefing: Orchestrator waking itself on a schedule to summarize the week ahead without any user prompt. The cron job infrastructure is ready. The specific content format is not yet defined.

**Multi-device Context Sync**
How `judgement_rules.md`, `north_stars.md`, and `episodic.db` stay consistent across multiple machines. Deferred until the system is stable on a single machine.

**Episodic Store Pruning**
`episodic.db` currently queries `ORDER BY timestamp DESC LIMIT 500` to keep retrieval fast.  A more principled pruning strategy (deduplicate similar summaries, age out low-relevance episodes) is not yet designed.

**Embedding Model Upgrade Path**
`openai/text-embedding-3-small` is the current embedding model.  If better models appear on OpenRouter or if the 1536-dimension vectors become expensive to store at scale, the `EmbeddingIndex` needs a migration path for existing vectors.  Not designed yet.

**Error Recovery and Context Correction**
Manual override of specific judgement rules, rollback of bad context deltas, reprocessing Ledger entries with a corrected extraction prompt. Mechanism not yet designed.  (Versioning infrastructure is the recommended first step.)

**Confidence Decay**
Judgement rules learned a long time ago may become stale as preferences change. The EMA scoring already gives more weight to recent observations, but a time-based decay model for very old rules is not yet designed.

**Persona Layer**
Loading mental models of specific thinkers as advisory lenses on Orchestrator decisions. Deliberately post v1.

**Offline Transcription Fallback**
Voice input depends on OpenRouter being reachable. A local Whisper variant (`mlx-whisper`) behind the same `InferenceRouter` interface would close this gap, but the switching policy is not yet defined.

**Private Context Request Flow**
Static access control is enforced: `Agent._load_context()` calls `_allowed_documents()` which reads `privacy_rules.md` before injecting any context into an agent's prompt. `private.md` is never included by default — only agents with an explicit `can_read: private.md` rule in `privacy_rules.md` can access it.

What is **not yet implemented** is the *dynamic* private context request described in §5.3: the flow where an agent mid-task raises a request through the Orchestrator, the user approves via an Approval card, and the agent gets temporary access to `private.md` for that session only. Until this is built, agents that need private data must have it pre-granted in `privacy_rules.md`, or the user must inject the relevant facts via `north context add`.

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
```

FastAPI handles:
- All REST endpoints defined in Section 6.8
- SSE stream via a custom `EventStreamManager` in `orchestrator/stream.py` using FastAPI's built-in `StreamingResponse` — no external SSE library
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

Seven SQLite databases:
```
~/.north/ledger.db       <- ledger entries (append-only)
~/.north/jobs.db         <- job queue + user cron entries
~/.north/tools.db        <- tool confidence scores (EMA-updated, consecutive_failures)
~/.north/embeddings.db   <- paragraph embedding vectors for context documents
~/.north/episodic.db     <- per-task episode summaries with embeddings
~/.north/tool_index.db   <- per-tool embedding vectors for semantic tool selection
~/.north/facts.db        <- per-fact embedding vectors for semantic context retrieval
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

### 16.10 Notifications

**TerminalNotifier (default on all platforms)**
The default `Notifier` implementation (`approval/terminal.py`) prints approval cards to stdout/logs. It works anywhere Python runs — Linux, macOS, Docker, CI.

**MacOSNotifier / AlerterNotifier (macOS only, optional)**
For native macOS notification banners with action buttons, `alerter` is called as a subprocess (`approval/macos.py`). `MacOSNotifier` and `AlerterNotifier` are aliases for the same class.

```bash
brew install vjeantet/tap/alerter  # one-time, macOS only
```

To enable, swap `TerminalNotifier()` → `MacOSNotifier(settings.secret)` in `config/dependencies.py`. The `Notifier` ABC (see `docs/CODING_STYLE.md` Section 6.1) hides this choice from the rest of the system.

### 16.11 Environment and Configuration

**Environment variables** for secrets and configuration that cannot be in the repo:

```bash
# Inference providers — set in ~/.north/.env
NORTH_OPENROUTER_API_KEY=sk-or-...          # required — broadest model catalogue
NORTH_GROQ_API_KEY=gsk_...                  # optional — fast free-tier completions + Whisper transcription
NORTH_GEMINI_API_KEY=AIza...                # optional — Gemini free-tier completions + embeddings

# System
NORTH_HOME=~/.north                         # optional, override data directory (e.g. /data in Docker)
NORTH_SECRET=your-secret                    # optional, override secret.key file (preferred in Docker)
NORTH_NORTH_ENV=development                 # development | production | test

# Tuning (all optional, defaults shown)
NORTH_JOB_POLL_INTERVAL_SECONDS=5          # how often the job processor wakes
NORTH_AGENT_READ_TIMEOUT_SECONDS=30        # timeout waiting for a key in Task Context Object
NORTH_TASK_CLEANUP_COMPLETED_DAYS=7        # retain completed task DBs for N days
NORTH_TASK_CLEANUP_FAILED_DAYS=30          # retain failed task DBs for N days
NORTH_INFERENCE_POOL_REFRESH_INTERVAL_HOURS=6  # how often the model registry is refreshed
NORTH_AGENT_MAX_ITERATIONS=40              # ReAct loop iteration cap per agent
NORTH_EXTRACTION_POLL_INTERVAL_SECONDS=120 # extraction pipeline check frequency
NORTH_EXTRACTION_MAX_DAILY_COST_USD=0.10   # daily spend cap for the extraction pipeline
```

`NORTH_HOME` and `NORTH_SECRET` are read directly from the environment (no doubled `NORTH_` prefix). All other tuning variables follow pydantic-settings' `NORTH_` prefix convention.

**`~/.north/secret.key`** is generated automatically on first `north start` if it does not exist. It is never committed to the repository. When `NORTH_SECRET` is set, the file is not consulted.

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

north runs on any platform (Linux, macOS, Windows via Docker). All deployments use `TerminalNotifier` by default. Native macOS notification banners require alerter and a local install (see Section 16.10).

#### Recommended: curl installer

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
north start
```

`scripts/install.sh` checks for Docker, installs `uv` if needed, installs the `north` CLI from GitHub, prompts for your OpenRouter API key, and saves it to `~/.north/.env`. After that `north start` is the only remaining step from any directory.

On first `north start`:
- `~/.north/` is created with all SQLite databases and `secret.key`
- The bundled `cli/docker-compose.yml` (which pulls `ghcr.io/kushagrabainsla/north:latest`) is copied to `~/.north/docker-compose.yml`
- Docker pulls the image and starts the container
- Your home directory is mounted inside the container so agents can access your files

```
★ north  Mode         → Docker Compose
         Web UI       → http://127.0.0.1:8000/ui/
         Workspace    → /Users/you
```

#### Manual install (alternative)

**Prerequisites:** Docker with the Compose plugin, Python 3.12+, `uv`.

```bash
uv tool install git+https://github.com/Kushagrabainsla/north
echo "NORTH_OPENROUTER_API_KEY=sk-or-your-key" >> ~/.north/.env
north start
```

#### Workspace

Agents can read and write files within a configured workspace. In Docker mode, the workspace defaults to `$HOME` (your entire home directory is mounted inside the container). Per-request workspace is resolved automatically:

```bash
cd ~/myproject
north task "review my code"   # workspace auto-detected as git root of ~/myproject
north chat                    # same — scoped to ~/myproject
north chat --workspace ~/other-project   # explicit override
```

`north start --workspace /some/path` overrides the default for the server session.

#### Verify from the CLI

In a second terminal while north is running:

```bash
north tasks          # list active tasks (empty when nothing is running)
north ledger         # recent ledger entries
north inference models  # current model pool state
```

#### Stop

```bash
north stop           # docker compose down (if Docker) or kills port 8000
```

#### Update

```bash
uv tool install git+https://github.com/Kushagrabainsla/north --force-reinstall
```

The Docker image is updated automatically on the next `north start` (it always pulls `latest`).

#### For developers (editable install with dev tools)

```bash
git clone https://github.com/Kushagrabainsla/north
cd north

# Install all dependencies including dev group (pytest, etc.)
uv sync

# Configure
cp .env.example .env
# Edit .env: NORTH_OPENROUTER_API_KEY=sk-or-...

# Run (auto-reload on file changes)
uv run north start --reload --local

# Run tests
uv run pytest tests/unit/

# Lint and format
uv run ruff check .
uv run ruff format .
```

Developers running from the cloned repo get Docker mode with a local build (`build: .` in the root `docker-compose.yml`) rather than the GHCR image.

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
rich = ">=13.0.0"               # terminal formatting for ledger output, agent status, cost tables

# Config
pyyaml = ">=6.0"                # agent config.yaml / tools.yaml parsing

# Document parsing
pypdf = ">=4.0.0"
python-docx = ">=1.0.0"
beautifulsoup4 = ">=4.12.0"

# Web search
ddgs = ">=1.0.0"                # DuckDuckGo search (no API key required)

# Voice input
sounddevice = ">=0.4.6"         # push-to-talk audio capture
pynput = ">=1.7.6"              # keyboard listener for push-to-talk hotkey
numpy = ">=1.26.0"              # sounddevice returns numpy arrays; cosine similarity
                                #   in EmbeddingIndex and EpisodicStore

# System
psutil = ">=5.9.0"              # cross-platform process and port management (north start/stop)
```

Scheduling has no external dependency: cron entries are `(hour, minute, weekday)` tuples and `jobs/scheduler.py` is a single asyncio background task (see Section 11.3).

---

*You set the destination. north handles the navigation. You live the journey.*
