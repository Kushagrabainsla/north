# Technical Features
> A reference for the most interesting engineering decisions in north.
> For the full system spec see [ARCHITECTURE.md](ARCHITECTURE.md).
> For the factory's orchestrator's talk see [FACTORY VIDEO](https://www.youtube.com/watch?v=ow1we5PzK-o)

---

## 1. ReAct Loop with Native Function Calling

**What:** `AgenticLLMAgent` runs a ReAct (Reason + Act) loop using the OpenAI-compatible tools API rather than JSON-in-text prompting.

**Why:** JSON-in-text requires the model to produce a raw JSON string matching a hand-crafted schema, then parsing it with `json.loads()`. This fails silently when models wrap output in markdown fences, produce partial JSON, or hallucinate tool names. Function calling offloads schema enforcement to the provider - the model receives typed function definitions and returns a structured `tool_calls` object.

**How:**

```
messages = [system_prompt, user_task]
tools    = [typed JSON Schema defs] + [delegate_task, request_approval, ask_user]

for _ in range(agent_max_iterations):   # configurable from settings, default 40
    compact history if approaching context window limit
    response = complete_with_tools(messages, tools, token_callback)

    if response.type == "message":
        stream tokens to SSE; return final answer          # done

    if response.type == "tool_calls":
        run read-only calls in parallel via asyncio.gather()
        run mutating calls one at a time under a workspace lock
        record confidence via ConfidenceTracker
        emit tool_called + tool_result SSE events
        append (assistant tool-call turn + tool results) to messages
        continue
```

Within one iteration, read-only tool calls run in parallel, while mutating calls run one at a time under a per-workspace lock so two edits to the same file cannot race. The `delegate_task`, `request_approval`, and `ask_user` tools are synthetic: they never touch the tool registry. They run a sub-agent coroutine or block on the shared user-interaction mediator (see Section 16).

---

## 2. Dynamic Model Pool Tiering

**What:** `inference/dispatcher.py` collects all models from all configured providers and assigns each a continuous `base_quality` score derived from its output price via `quality_from_cost()` in `inference/capability.py`.

**Why:** hardcoding model names creates maintenance overhead and breaks silently when models are renamed or retired. Dynamic scoring means the system always uses the best currently-available models without any manual action.

**How:**

```python
# inference/capability.py
def quality_from_cost(cost_per_token: float) -> float:
    """Log-scale normalisation over the ~$0.000001–$0.015/token pricing range."""
    if cost_per_token <= 0:
        return _FREE_MODEL_QUALITY   # 0.35 floor for free models
    log_cost = math.log10(cost_per_token)
    normalised = (log_cost - _QUALITY_LOG_MIN) / (_QUALITY_LOG_MAX - _QUALITY_LOG_MIN)
    return max(0.0, min(normalised, 1.0))
```

`current_pools()` bins models into named tiers for CLI display using fixed thresholds:

```python
# _QUALITY_TIER_HIGH = 0.70, _QUALITY_TIER_MEDIUM = 0.40  (inference/constants.py)
if info.base_quality >= _QUALITY_TIER_HIGH:   # → "reasoning"
elif info.base_quality >= _QUALITY_TIER_MEDIUM: # → "fast_cheap"
else:                                           # → "high_volume"
if info.is_free:                               # also → "free_fallback"
```

Actual candidate ranking within each strategy blends `base_quality` with a live per-model EMA success rate via `_effective_quality()`, so a historically reliable cheap model can rank above an expensive one that has been failing.

---

## 3. Multi-Model Fallback Chain with Strategy Modes

**What:** every inference call walks an ordered model list. Any error advances to the next model. The chain ends only when all models are exhausted.

**Why:** single-model calls fail silently under rate limits, credit exhaustion, or model retirement. A fallback chain makes the system self-healing without user intervention.

**Three strategies** (set via natural language or `POST /orchestrator/settings`):

| Strategy | Model ordering | Use case |
|---|---|---|
| `eco` | cheapest first, climb on failure | minimise cost |
| `cruise` | honour the requested priority, then rank candidates by quality and live success rate | balanced default |
| `sport` | most capable first, descend on failure | maximise quality |

**Cruise ranking** (`priority=HIGH`): candidates from the allowed pool are ranked by the strategy and by effective quality, which blends base quality with a live success rate. It is not a fixed tier ladder.

**Exception classes that advance the chain** (none of them stops it):

- `ModelRateLimitedError` - HTTP 429/404/503. A cooldown is applied, logged at `INFO`.
- `PaymentRequiredError` - HTTP 402. A longer payment cooldown is applied, logged at `WARNING`.
- `InferenceError` - HTTP 400, bad model ID, unsupported parameters. Logged at `WARNING`.

`AllModelsRateLimitedError` is raised only when the entire ordered list is exhausted.

---

## 4. Error-Triggered Pool Refresh with Cooldown

**What:** on a retryable agent failure, `_maybe_refresh_pools_background()` schedules a background pool refresh, subject to a 60-second cooldown.

**Why:** a 404 from a retired model ID is a signal that the local pool cache is stale. Refreshing immediately means the next call uses current model IDs rather than continuing to hammer dead endpoints. The cooldown prevents a storm of refresh calls if many models fail in quick succession.

**Pool refresh on startup + loop:** `orchestrator/app.py` calls `refresh_pools()` once explicitly during the lifespan startup before yielding to the server.  A background loop then sleeps for `inference_pool_refresh_interval_hours` (default 6 h) between subsequent refreshes:

```python
# lifespan startup (orchestrator/app.py)
await deps.inference_router.refresh_pools()   # immediate, before first request

# background loop (_pool_refresh_loop)
async def _pool_refresh_loop(deps) -> None:
    interval = settings.inference_pool_refresh_interval_hours * 3600
    while True:
        await asyncio.sleep(interval)         # sleep first, then refresh
        await deps.inference_router.refresh_pools()
```

**Error-triggered refresh** (with cooldown):

```python
async def _maybe_refresh_pools_background(self) -> None:
    now = time.monotonic()
    if now - self._last_pool_refresh_at < POOL_REFRESH_COOLDOWN:
        return
    self._last_pool_refresh_at = now
    spawn(self._deps.inference_router.refresh_pools(), name="pool_refresh")
```

---

## 5. EMA Tool Confidence Scoring

**What:** every tool edge in the tool graph carries a confidence score from 0.0 to 1.0 updated by an exponential moving average after every use.

**Why:** the old fixed-delta approach (`+0.05 / -0.03`) took ~27 successful uses to recover a low-scoring tool. EMA with a base alpha of 0.10 recovers in ~10 successful uses, giving recent behaviour much more weight. On consecutive failures the alpha grows so repeated failures lower confidence faster.

**Formula:**

```python
# base alpha on success; on consecutive failures alpha scales up:
# 0.10 -> 0.20 -> 0.40 (capped at 0.50)
alpha = 0.10 if was_helpful else min(0.50, 0.10 * 2 ** min(consecutive_failures, 2))
outcome = 1.0 if was_helpful else 0.0
new_confidence = clamp(alpha * outcome + (1 - alpha) * current_confidence, 0.0, 1.0)
```

**Persistence:** scores live in `~/.north/tools.db`. The read-modify-write runs in one atomic transaction (`BEGIN IMMEDIATE`) so two concurrent updates for the same (agent, tool) cannot clobber each other. On startup, reliable filesystem/shell tools are seeded at 0.80 via `seed_defaults()`. New agent pairs start at 0.50. A new agent can declare `similar_to: health` in `config.yaml` to inherit the health agent's confidence rows as its prior.

**Effect on the agent loop:** when a tool index is available, the agent injects the top-k tools that are semantically relevant to the task, then sorts them by confidence descending. It falls back to the full tool list when the index is unavailable.

---

## 6. Context Window Compaction with LLM Summarization

**What:** `agents/context_compaction.py:compact_if_needed()` monitors `tokens_in` before each ReAct iteration and summarizes old tool-call exchanges when usage exceeds 75% of the model's context window.

**Why:** long-running agents accumulate large tool outputs in their message history. Truncating naively loses important facts (file paths, error messages, function names). LLM summarization preserves the semantically important content while dramatically reducing token count.

**Algorithm:**

```
if tokens_in < context_window * 0.75:
    truncate older tool outputs in-place (fast path)
    return

exchanges = list of (assistant tool-call turn + tool result turns)
keep last N exchanges verbatim
summarize everything before them via a LOW-priority inference call
replace old exchanges with:
  {"role": "user",      "content": "## Earlier context (auto-compacted)\n<summary>"}
  {"role": "assistant", "content": "Understood - I have the compacted context."}
```

The summary call uses `PoolPriority.LOW` so it doesn't compete with the main agent call. Falls back to truncation-only if the summary call fails.

**Context window table** (`_CONTEXT_WINDOW_TABLE`) maps model name fragments to their published window sizes, covering Gemini (1M), Claude (200K), GPT-4o (128K), Phi (16K), etc. Agents with heavy-output tools (`bash`, `git`, `patch_file`) get a larger summary token budget (1000 vs 512 tokens).

---

## 7. Real-Time Token Streaming via SSE

**What:** `complete_with_tools()` streams the model's text response token-by-token to an async callback, which emits `token` SSE events to connected north clients (the CLI stream and the TUI).

**Why:** without streaming, the client sees nothing until the full response is assembled server-side. Streaming gives the user progressive rendering. The response appears word by word as the model generates it, just like a native chat interface.

**Implementation:** `OpenAICompatibleProvider.complete_with_tools()` uses `httpx.AsyncClient.stream()` and processes each `data: {...}` SSE chunk. Text token deltas go to `token_callback` immediately. Tool call argument chunks are accumulated in a dict until `[DONE]`.

```python
async with self._client.stream("POST", "/chat/completions", json=body) as resp:
    async for raw_line in resp.aiter_lines():
        chunk = json.loads(raw_line[6:])          # strip "data: "
        delta = chunk["choices"][0]["delta"]
        if text_token := delta.get("content"):
            await token_callback(text_token)       # → SSE "token" event
        for tc in delta.get("tool_calls", []):
            tool_calls[tc["index"]]["arguments"] += tc["function"].get("arguments", "")
```

---

## 8. Semantic Context Search with Cosine Similarity

**What:** `context/embedding_index.py:EmbeddingIndex` stores paragraph-level embedding vectors for the five context documents. `FileContextStore.search()` uses cosine similarity to retrieve the top-k relevant paragraphs.

**Architecture:**

```
write()/append() call
  → spawn(re-index updated document, name="reindex")
      → chunk document into paragraphs
      → InferenceRouter.embed(paragraphs) in one batch call
      → INSERT INTO embeddings.db (doc, chunk_idx, text, vector)

search(query)
  → embed(query)  [1 API call]
  → SELECT all vectors FROM embeddings.db
  → cosine_similarity(query_vec, each stored vec) via numpy
  → return top-k paragraphs with [Source Document] labels
```

**Fallback:** if `EmbeddingIndex` is absent or the embed call fails, `search()` falls back to paragraph-level keyword overlap scoring (already implemented). Callers always get a result regardless of embedding availability.

**Embedding model:** `openai/text-embedding-3-small` via OpenRouter - same API key as inference, no extra dependency.

---

## 9. Episodic Memory Layer

**What:** after every completed task, the Orchestrator writes a (prompt, output) summary to `~/.north/episodic.db` with an embedding vector. Before each agent run, the top-3 most semantically similar past episodes are injected into the agent's context block.

**Why:** agents have no cross-session memory by default. Without episodic recall, the job agent would re-research the same companies every time. Episodic injection gives agents continuity without burdening the context window with the full ledger.

**Storage schema:**
```sql
CREATE TABLE episodes (
  id        TEXT PRIMARY KEY,
  task_id   TEXT,
  domain    TEXT NOT NULL,
  summary   TEXT NOT NULL,      -- "Task: <200 chars>\nResult: <500 chars>"
  embedding TEXT,               -- JSON float array, null if embed failed
  timestamp TEXT NOT NULL
)
```

**Retrieval:** cosine similarity (numpy) over all stored vectors; keyword fallback when embeddings are unavailable. Episodes are pruned on write: rows older than 90 days are deleted, then the oldest rows are trimmed so the store stays within a fixed cap. This keeps retrieval fast as the store grows.

---

## 10. Structured Error Classification

**What:** `orchestrator/failure_handler.py:classify_error()` maps any Python exception to one of seven stable string tags before any retry or notification logic runs.

**Why:** retry strategies differ by error type. A `rate_limit` needs a cooldown. A `network` error should retry immediately. A `logic_error` should never retry. Without explicit classification, all errors collapse into a single "failed" bucket and you can't distinguish them in the Ledger.

**Tag taxonomy:** tags are assigned by ordered heuristics over the exception's type and message text, not a strict status-code table.

| Tag | Typical signals |
|---|---|
| `rate_limit` | 429, 402, `AllModelsRateLimitedError` |
| `context_overflow` | 400 with "context length" in message |
| `timeout` | `asyncio.TimeoutError`, `httpx.TimeoutException` |
| `network` | `httpx.RequestError`, `ConnectionError` |
| `parse_error` | `json.JSONDecodeError`, `ValidationError` |
| `config_error` | missing key, bad config, `AgentConfigError` |
| `logic_error` | everything else |

The tag is written to `LedgerEntry.error_type`, making failure patterns queryable:
```bash
north ledger --error-type rate_limit --agent finance
```

---

## 11. asyncio.Event-Based Approval Waiting

**What:** `ApprovalStore.wait_for_decision()` suspends a coroutine on an `asyncio.Event` until the user responds to an approval card, rather than polling in a loop.

**Why:** a polling approach holds the event loop busy and adds 0–1 s latency to every approval. With `asyncio.Event`, zero CPU is consumed while waiting - the coroutine is simply suspended until the specific event fires.

**Before (polling):**
```python
for _ in range(300):
    await asyncio.sleep(1)                         # 300 wakeups, ~1 s latency
    if approval_store.get(card_id).status != "pending":
        break
```

**After (event-based):**
```python
card = await approval_store.wait_for_decision(card_id, timeout=300.0)
# wakes exactly when resolve() is called - zero CPU while waiting
```

**Implementation:** `ApprovalStore.add()` allocates a `asyncio.Event` per card. `resolve()` calls `event.set()`. `wait_for_decision()` uses `asyncio.wait_for(event.wait(), timeout=300.0)`. Under load with many concurrent pending approvals (e.g., multiple parallel agent tasks each waiting for sign-off), each coroutine is independently suspended with no shared state contention.

---

## 12. CI/CD Pipeline

**What:** two workflow files in `.github/workflows/` cover the full release lifecycle.

### `ci.yml` - Lint and Test

- **Parallel jobs:** `lint` (ruff) and `test` (pytest) run as independent jobs. Either can be re-run alone. Branch protection can require them separately.
- **Caching:** `astral-sh/setup-uv@v8` handles uv dependency caching keyed on `pyproject.toml`. Cache hits skip the full install on subsequent runs.
- **Concurrency:** `cancel-in-progress: true` kills the stale run on the same ref when a newer commit is pushed, so PRs never queue behind their own old runs.
- **Timeouts:** `timeout-minutes: 5` on lint, `15` on tests - hung runners are killed automatically.

### `docker-publish.yml` - Build and Push

- **Multi-platform:** `linux/amd64` and `linux/arm64` built in a single `docker/build-push-action@v7` call via QEMU emulation (`docker/setup-qemu-action@v4`).
- **Layer cache:** `cache-from/cache-to: type=gha` reuses Docker layer cache across workflow runs via the GitHub Actions cache backend. After the first push, unchanged layers are never rebuilt.
- **SBOM + provenance:** `sbom: true` and `provenance: true` embed a software bill of materials and BuildKit provenance attestation directly in the image manifest.
- **SLSA attestation:** `actions/attest@v4` creates a GitHub Artifact Attestation signed with a GitHub-issued OIDC token. Verifiable with:
  ```bash
  gh attestation verify oci://ghcr.io/kushagrabainsla/north:latest \
    --owner Kushagrabainsla
  ```
- **Concurrency:** `cancel-in-progress: false` - a publish in flight on a tag is never interrupted.
- **Tagging:** branch name, `{{version}}`, `{{major}}.{{minor}}`, and `latest` (on default branch only) are all produced in a single metadata step.

---

## 13. Three-Layer BashTool Command Safety

**What:** `BashTool._request_approval()` evaluates every shell command through three progressively heavier gates before execution. If an earlier gate produces a decision, later gates are skipped entirely.

**Why:** Without any bypass, every `git status` or `cat README.md` blocks on a manual approval card - adding 5–30 s of human latency to pure read-only operations. The three-layer design keeps developers in flow for safe commands while still gating anything risky.

**Layers (evaluated in order):**

| Layer | Class | Cost | Decision |
|---|---|---|---|
| 1. Local inspection | `CommandSafetyInspector` | Zero, local only | Auto-approve read-only commands (`git status`, `cat`, `ls`, `grep`, etc.) after metacharacter, recursive-grep, and sensitive-path screening |
| 2. Learned rules | `JudgementFilter` | One LLM call against `judgement_rules.md` | Auto-approve/reject based on patterns the user has established through prior approvals |
| 3. Manual approval | `ApprovalStore` card | Human decision | Fallback for unknown or mutating commands |

**Layer 1 - `CommandSafetyInspector`:**

```python
class CommandSafetyInspector:
    instant_safe_prefixes = [
        "git status", "git diff", "git log", "git show", "git branch",
        "cat ", "grep ", "ls ", "pwd", "whoami",
    ]  # note: `find` is NOT here; it can walk trees and run actions

    def is_instantly_safe(self, command: str) -> bool:
        cleaned = command.strip()
        if _SHELL_METACHARS.search(cleaned):           # ; & | ` $ < > ( ) { } newline
            return False
        if not any(cleaned.lower().startswith(p) for p in self.instant_safe_prefixes):
            return False
        if cleaned.lower().startswith("grep ") and _grep_is_recursive(cleaned):
            return False
        return not references_sensitive_path(cleaned)  # blocks ~/.ssh, /etc, and .. escapes
```

This is **not a security boundary** - it's a developer-velocity optimisation. The list intentionally covers only commands that cannot mutate the filesystem, push to remotes, or spawn network requests.

**Layer 2 - `JudgementFilter` (existing system):**

If the command is not instantly safe, `BashTool` forwards an approval card to `JudgementFilter.check()`. The filter compares the card against learned rules from `judgement_rules.md` (populated by the extraction pipeline from prior user approvals). If a matching rule exists, the command is auto-approved or auto-rejected with no human prompt.

**Layer 3 - Manual approval card:**

If both Layer 1 and Layer 2 are inconclusive, a standard approval card is emitted and the coroutine suspends on `ApprovalStore.wait_for_decision()` until the user responds (see §11).

**Dependency injection:** `JudgementFilter` is instantiated once during server startup in `orchestrator/app.py` and shared between the `Orchestrator` (for general approvals) and `BashTool` (for command-specific approvals). `CommandSafetyInspector` is a zero-dependency value object created internally by `BashTool.__init__()`.

The same approval flow is shared by every gated tool (`BashTool`, `ShellTool`, `PatchFileTool`, `GitTool`, `GhTool`, `KasaTool`) through the one `UserInteraction` mediator (see Section 16), so the tools never drift.

## 14. Persistent PTY Shell Sessions

**What:** `ShellTool` (`tools/specialized/shell_tool.py`) keeps a process alive across tool calls behind a pseudo-terminal. Where `BashTool` is one-shot (run → capture → exit), `ShellTool` exposes `start` / `read` / `write` / `stop` / `list` so an agent can launch `npm run dev`, `tsc --watch`, a REPL, or a debugger, then stream output, send input, and terminate it over several iterations.

**Why:** Many real coding tasks need a long-running process - start a dev server then curl it, watch a compiler, drive an interactive REPL. A one-shot subprocess cannot express any of these.

**How:**
- Each session is backed by `pty.openpty()` (stdlib - no third-party dependency like `pexpect`; the model does the "wait for X" reasoning itself, so an expect layer adds nothing).
- The PTY master fd is registered with the event loop via `loop.add_reader()`; output accumulates in a per-session ring buffer (`_MAX_BUFFER_BYTES`) drained on each `read`.
- Processes spawn with `start_new_session=True` so `stop` can `killpg` the whole group (SIGTERM, then SIGKILL on timeout).
- Safety mirrors `BashTool`: `start` and `write` go through the shared approval flow; `read` / `stop` / `list` operate on an already-approved session. A session cap (`_MAX_SESSIONS`) bounds runaway shells.

## 15. Diff Preview Before Write

**What:** When an `ApprovalStore` is injected, `PatchFileTool` computes the would-be new file content, renders a unified diff (`difflib`), and surfaces it in an approval card. The write happens only on confirm; a rejection leaves the file untouched.

**Why:** It turns north's approval layer into a true review gate for edits - the user sees exactly what changes before it lands, rather than approving a blind "edit file" action. This plays to north's differentiator (the approval layer + ledger) rather than copying per-tool permission prompts.

**How:**
- Computation is split from writing: `_plan()` performs no writes. It reads the current file contents and returns `(new_content, old_content, blocks_applied)` for all three edit shapes (`edits` list, `old_string`/`new_string`, SEARCH/REPLACE blocks).
- A no-op edit (`new_content == old_content`) short-circuits to success without prompting.
- The injected, diff-previewing instance is registered in `orchestrator/app.py`, overriding the auto-discovered no-arg instance by name. Without a store (e.g. unit tests) the tool applies immediately, which keeps it backward compatible.

---

## 16. Unified User Interaction Mediator

**What:** `approval/interaction.py:UserInteraction` is the single path for every user-facing card: approvals, questions, and information. Tools, agents, and the orchestrator all go through it.

**Why:** the surface-then-await sequence used to be reimplemented in three places, which let them drift. For example, one path decided "approved" by matching a button label while another used the card status. One mediator removes the duplication and gives every card the same behavior.

**How:** for each card it applies the learned `JudgementFilter` (which can auto-resolve), registers the card in the `ApprovalStore`, emits the SSE event, fires the TUI-aware `Notifier`, and for decisions it blocks on `wait_for_decision`. The decision is read from the card's `status` (approved or rejected), never from a button label. A timeout resolves the card as `timeout_rejected`. Each caller passes only the dependencies it has: the orchestrator wires a notifier and a ledger audit hook, while tools and agents pass the stream manager.

---

## 17. Supervised Background Tasks

**What:** `utils/tasks.py:spawn(coro, *, name)` is the one way north launches a background coroutine it does not await.

**Why:** a bare `asyncio.create_task(...)` can be garbage-collected before it finishes, and any exception it raises vanishes silently. Several places had hand-rolled "create a task and attach a logging callback" code that did the same thing in slightly different ways.

**How:** `spawn` keeps a strong reference to the task until it completes and attaches a done-callback that logs any exception under the given name. Cancellation is logged at debug, not as an error. Re-indexing, episode recording, ledger writes, confidence recording, and pool refresh all use it.

