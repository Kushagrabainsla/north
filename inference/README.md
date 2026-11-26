# inference

The `inference` package connects north to language models. It sends chat completions, tool-calling
turns, embeddings and audio transcription to a set of providers. The rest of north uses only the
`InferenceRouter` interface, and `ModelDispatcher` implements it. For each completion, the chain
router in `routing/` selects the model from facts that `facts/` downloads from catalog sources.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `InferenceRouter` (ABC) | The interface for all callers: `complete`, `complete_with_tools`, `embed`, `transcribe`, `refresh_catalog`, `models_by_capability`. |
| `dispatcher.py` | `ModelDispatcher` | Implements `InferenceRouter`. Holds the providers and refreshes their catalogs. Keeps cooldowns, provider health, rate-limit status and the per-model success and speed averages. Sends completions to the chain router. Selects embedding and transcription models itself. |
| `factory.py`, `runtime.py` | `build_router`, `build_inference_router_from_settings`, `rebuild_runtime_router` | Make a `ModelDispatcher` from the provider registry and the live settings. |
| `registry.py` | `PROVIDER_DEFINITIONS`, `ProviderDefinition`, `AuthKind`, `get_provider_definition` | The one list of provider integrations: id, auth kind, key name, setup URL, fallback order and factory. |
| `provider.py` | `Provider` (Protocol) | The contract for one provider. Each method gets an explicit `model_id`. |
| `providers/` | `OpenAICompatibleProvider`, `GroqRouter`, `GeminiRouter`, `OpenRouterRouter`, `OpenCodeZenRouter`, `MistralRouter`, `AnthropicProvider`, `OpenAICodexProvider`, `LocalEmbeddingProvider`, `OnnxTextEncoder` | One file per provider. Most subclass `OpenAICompatibleProvider` (`openai_compat.py`). `anthropic_provider.py` uses the `anthropic` SDK. `openai_codex.py` uses the Responses protocol. `local_embeddings.py` runs `BAAI/bge-small-en-v1.5` on the device through `onnx_encoder.py`. |
| `routing/` | `ChainRouter`, `build_chain`, `ChainWalk`, `PartProfile`, `profile_for`, `kind_of`, `AvailabilityView`, `EntitlementLedger` | Selects the model for one part of a task. `parts.py` maps a `component` label to a profile. `chain.py` puts every model that qualifies in one list, best first, and walks it. `availability.py` tells if an endpoint can be called now, and why not. `router.py` runs the walk and records the decision. |
| `facts/` | `FactsCatalog`, `CatalogSnapshot`, `ModelFactsStore`, `ModelFacts`, `Endpoint`, `Rank`, `canonical`, `merge_all` | Downloads model facts (OpenRouter, LiteLLM and the provider catalogs, in `sources/`). Joins them on a canonical model id, merges them by rank and keeps them in `models.db`. Routing reads only the in-memory `CatalogSnapshot`. |
| `decisions.py` | `DecisionLog`, `RoutingDecision` | One row per model selection: the part, its requirements, each skipped model with its reason, the winner and the result. |
| `failure.py` | `Scope`, `Failure`, `classify`, `OutageCorroboration` | Decides who a failure applies to: one request, one model, one account tier or the full provider. |
| `cooldowns.py`, `provider_health.py`, `rate_limit_status.py` | `CooldownStore`, `ProviderHealthTracker`, `RateLimitStatusStore`, `format_status_table`, `format_status_markdown` | Per-model cooldowns, a per-provider circuit breaker, and rate-limit records with the provider's own reset time. |
| `cost_tracker.py` | `CostTracker`, `inference_call_category` | An `InferenceRouter` decorator. Adds up `cost_usd` per task and reports each call to a sink. |
| `models.py` | `CompletionRequest`, `CompletionResponse`, `ToolCallRequest`, `ToolCallResponse`, `EmbedRequest`, `TranscriptionRequest`, `ModelGroup`, `CostSummary`, `EmbedFn` | Pydantic request and response models, and the function types that memory uses. |
| `exceptions.py` | `InferenceError` and subclasses, `is_model_unavailable_error`, `missing_resource`, `WaitingFor` | Typed failures. `missing_resource` tells a missing model or network apart from a real failure. |
| `capability.py`, `model_policy.py` | `ModelCapability`, `ModelInfo`, `quality_from_cost`, `split_spec`, `model_matches` | Per-model metadata. Matches a written model spec (for example `groq:qwen3-32b`) to a catalog entry. |
| `auth.py`, `codex_auth.py` | `CredentialProvider`, `ApiKeyCredentialProvider`, `CodexCredentialProvider`, `CodexTokenStore` | Credentials, kept apart from the wire protocol. Codex uses a PKCE browser login and north stores the token. |
| `usage.py`, `cache_stats.py`, `transcript.py`, `durations.py`, `constants.py` | `cache_tokens`, `CacheWasteTracker`, `message_text`, `system_notes_in_place`, `parse_duration_seconds` | Small shared helpers: cache-token counts, likely cache misses, message shape, retry-time parsing, base URLs and tuning values. |

## How north uses it

- `config/dependencies.py` calls `build_inference_router_from_settings` and wraps the result in
  `CostTracker`. The call sink writes one `INFERENCE_CALL` ledger row per model call.
- `orchestrator/app.py` calls `refresh_catalog()` at startup and then every
  `inference_pool_refresh_interval_seconds` (180 by default).
- `agents/agentic_llm_agent.py` sends `ToolCallRequest` turns. It catches `ContextTooLargeError`
  and `AllModelsRateLimitedError`. `agents/context_compaction.py` and `agents/llm_agent.py` send
  `CompletionRequest`.
- `memory/` (extraction, consolidator, injection), `approval/decider.py`, `skills/distiller.py`
  and `orchestrator/north_star.py` call `complete()`.
- `orchestrator/orchestrator.py` reads and clears the cost of each task with
  `CostTracker.pop_task_cost`. `orchestrator/result_audit.py` sends its critic calls through the
  same `CostTracker`.
- `web/api.py` and `tools/specialized/north_config.py` call `rebuild_runtime_router` after a key
  changes, then `CostTracker.set_inner`. `web/api.py` and `cli/main.py` read
  `PROVIDER_DEFINITIONS` for provider setup.
- CLI: `north routing` reads `DecisionLog`, and `north limits` calls `format_status_table`.
  `gateways/telegram.py` calls `format_status_markdown`.
- Public interface: `__all__` in `inference/__init__.py`. Most callers also import
  `inference.models`, `inference.exceptions` and `inference.registry` directly.

## Rules

- Depend on `InferenceRouter`, never on a `Provider` or on `ModelDispatcher`
  (CODING_STYLE §6.1). Only the dispatcher calls providers.
- Set `component` on each request. The chain comes from that part's profile and the request. There
  is no per-call priority. If a new part is not task work, add it to `kind_of` (CODING_STYLE §17.3).
- Model choice reads fetched facts, never a model name or a tier table. To add a catalog source,
  add one parser file in `facts/sources/`.
- `canonical()` removes only known variant suffixes, never version or size tokens. A wrong merge
  sends work to a model that the caller did not choose.
- Price and limits come only from the `Endpoint` of the provider that serves the call. Facts can
  cross providers. Terms cannot.
- A wider failure scope needs more evidence. `Scope.PROVIDER_DOWN` comes only from failures on
  different models (`OutageCorroboration`).
- Catch `httpx` errors in the provider and raise an `InferenceError` subclass (CODING_STYLE §13.4).
- A missing model, network or credit pauses work. It is not a task failure. Use `missing_resource`
  and `WaitingFor` (CODING_STYLE §13.5).
- The inference hot path does not read SQLite. `models.db` is the restart and offline cache.
- All vector stores need one embedding model. The `local` provider has fallback order 1, so it
  comes first (see `utils/vector_space.py`).
- To add a provider, write a class that meets `Provider` and add a `ProviderDefinition` to
  `PROVIDER_DEFINITIONS`.
- Put type hints on everything. CI runs mypy on `inference/` (CODING_STYLE §9.2,
  `.github/workflows/ci.yml`).
- Add tests in `tests/unit/inference/` with the change (CODING_STYLE §18.8).

## Data on disk

All files are in north's home (`~/.north` by default).

- `models.db`: model facts and endpoints (`ModelFactsStore`), and the `routing_decisions` table
  (`DecisionLog`). This is not `facts.db`, which holds user memory.
- `cooldowns.json`: payment cooldowns that must survive a restart.
- `rate_limit_status.json`: rate-limit records with reset times.
- `cache/litellm_models.json`: the LiteLLM table, fetched once a day.
- `credentials/openai_codex.json`: the Codex OAuth token.
- `tools.db`: the per-model success and speed averages, kept through
  `tools.confidence.ConfidenceTracker`.
