# Clean Architecture Refactor Status

This ledger is the durable execution record for [the approved refactor program](CLEAN_ARCHITECTURE_PLAN.md). It is updated in the same change as each completed logical unit.

## Rules

- Do not mark a unit complete without command output or other concrete validation evidence.
- Record pre-existing failures separately from regressions.
- Do not advance a stage while a regression or unresolved blocker remains.
- Each completed logical unit must name the commit pushed to `origin/refactor/clean-architecture`.

## Stage 0 — Safety baseline and control plane

**Status: Complete.** Baseline evidence, ADRs, a durable plan/status ledger, a formatter repair, and an sdist packaging guard are committed and pushed. The one non-fatal setuptools warning about `web/src` is deferred to the frontend/package-layout stage.

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Persist approved plan and status ledger | Complete | `a265963` | `git diff --check`; `uv run ruff check .`; `uv run pytest` — 1,956 passed, 3 skipped | The initial format check exposed two pre-existing test formatting discrepancies; the isolated repair is recorded below. |
| Repair pre-existing formatter violations | Complete | `2a26984` | `uv run ruff format --check .`; `uv run ruff check .`; targeted pytest — 13 passed; full pytest — 1,956 passed, 3 skipped | Behavior-preserving ruff formatting only. |
| Exclude vendored frontend dependencies from sdist | Complete | `aaa1aab` | `uv build`; sdist inspection — 0 `web/node_modules` entries; wheel archive integrity check; `uv run pytest` — 1,956 passed, 3 skipped | The sdist shrank from 4,288,297 to 1,013,863 bytes. CI now asserts the exclusion. |
| Capture backend/frontend/packaging baseline | Complete | `aaa1aab` | Backend: ruff format/check pass; pytest — 1,956 passed, 3 skipped. Inference mypy — no issues in 45 files. Frontend: 9 tests passed; production build passed. Packaging: `uv build` passed. CLI help and sanitized Docker Compose syntax passed. | Existing non-fatal setuptools warning: TypeScript `web/src` is detected as a package-data directory. Address during the frontend/package-layout migration, not in this focused baseline. |
| Add ADR index and baseline decision records | Complete | `d7adf38` | ADR links, ruff format/check | ADRs 0001–0003 define enforceable module scopes, composition-root-only wiring, and compatibility-first staged migration. |

## Stage 1 — Architecture map and ownership contracts

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Define current-state module catalog and transitional dependency policy | Complete | `b63bf9b` | YAML ownership validation — 18 modules own 378 tracked production paths exactly once; ruff format/check; full pytest — 1,956 passed, 3 skipped | The catalog preserves existing paths while defining owners, contracts, protected surfaces, and temporary dependency exemptions. |
| Correct architecture repository map | Complete | `13a3f1e` | Relative Markdown links; ruff format/check | Replaced obsolete package/API/agent layout details with the current tree and linked the ownership catalog. |
| Validate every production path has one module owner | Complete | `b63bf9b` | Manifest validation — 18 modules own 378 tracked production paths exactly once | The equivalent check is now becoming a committed regression test in Stage 2. |

**Stage 1 status: Complete.** The current repository has an accurate map, a durable module catalog, one owner per tracked production path, declared temporary exceptions, and explicit protected surfaces.

## Stage 2 — Enforce boundaries and edit permissions

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Load and validate module contracts in code | Complete | `640fc64` | Targeted pytest — 3 passed; full pytest — 1,959 passed, 3 skipped; ruff format/check; mypy — no issues in 2 files | `architecture.contracts` resolves sole ownership and protects the manifest from malformed contracts. |
| Enforce dependency direction with AST tests | Complete | `0bfd003` | Architecture pytest — 6 passed; full pytest — 1,962 passed, 3 skipped; ruff format/check; mypy — no issues in 3 files | AST analysis rejects new cross-module layer violations while `architecture/import-baseline.txt` makes 27 existing forbidden pairs explicit for staged removal. |
| Define fail-closed task edit-scope policy | Complete | `37302e2` | Targeted pytest — 3 passed; full pytest — 1,965 passed, 3 skipped; ruff format/check; mypy — no issues in 4 files | `TaskEditScope` authorizes ordinary module/path edits and requires an explicit user-authorized path for protected modules. |
| Enforce task edit scopes in source-mutating tools | Complete | `3c264d4` | Full pytest — 1,986 passed, 3 skipped; ruff format/check; architecture mypy — no issues in 4 files | Server-owned scopes now guard direct writers, direct-tool execution, and atomic multi-file semantic renames; denied edits fail before any mutation. |

## Stage 3 — Composition root

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Inject web Codex credential-provider factory | Complete | `2e15e2c` | Full pytest — 1,998 passed, 3 skipped; ruff format/check; architecture mypy; import-boundary regression | `orchestrator.app` now supplies the web OAuth factory; web routes no longer directly construct the provider, while CLI auth remains an offline local adapter. |
| Centralize runtime inference-router construction | Complete | `98e1a7b` | Full pytest — 1,994 passed, 3 skipped; ruff format/check; architecture mypy; import-boundary regression | `inference.runtime` is the shared intelligence-layer factory; startup remains assembled in `config.dependencies.py`, while web/config reload paths no longer duplicate concrete router wiring. |

## Stage 4 — Remove reverse dependencies

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Replace UpdatePlanTool's concrete PlanStore dependency with a port | Complete | `2ff03d9` | Full pytest — 1,998 passed, 3 skipped; ruff format/check; architecture import-boundary regression | `tools.universal.update_plan` now consumes the platform `PlanStorePort`; `orchestrator.app` still injects the concrete `PlanStore`. |
| Replace concrete event-stream dependency with EventEmitter port | Complete | `f73e923` | Full pytest — 2,003 passed, 3 skipped; ruff format/check; architecture mypy | Tools emit through the dependency-light `utils.events.EventEmitter` protocol rather than importing orchestration streaming internals. |
| Replace active-session concrete dependency with a port | Complete | `b13a9a2` | Full pytest — 2,008 passed, 3 skipped; ruff format/check; architecture mypy | `GetActiveSessionsTool` consumes `utils.sessions.ActiveSessionStorePort`, eliminating the final integrations.tools → application.orchestration edge. |
| Move handoff lifecycle policy to platform utilities | Complete | `e65c974` | Full pytest — 2,013 passed, 3 skipped; ruff format/check; architecture mypy | `utils.handoff` owns dependency-light handoff paths; `tools._path` preserves compatibility re-exports. |
| Move weekday parsing to platform utilities | Complete | `32f9b78` | Full pytest — 2,038 passed, 3 skipped; ruff format/check; architecture mypy | Scheduling and cron retain the same parser object via compatibility re-exports from `utils.weekdays`. |
| Introduce type-only tools planning ports | Complete | `8f98bf6` | Full pytest — 2,043 passed, 3 skipped; ruff format/check; architecture mypy | Application planning code depends on `utils.tools` protocols instead of integration tool types. |
| Move filesystem pruning policy to platform utilities | Complete | `5316e76` | Full pytest — 2,048 passed, 3 skipped; ruff format/check; architecture mypy | `PRUNED_DIRS` has one platform owner while legacy imports retain object identity. |
| Remove application.orchestration → integrations.tools runtime tool-dispatch dependency | Complete | `5cb5772` | Full pytest — 2,059 passed, 3 skipped; ruff format/check; architecture mypy | Orchestration dispatches through `utils.tools` ports; concrete `ToolInput` and `ToolRegistry` are injected solely at `orchestrator.app`. The baseline pair and its Stage 4 manifest exemption are removed. |
| Remove platform.config → composition.app type-only dependency | Complete | `5349582` | Full pytest — 2,059 passed, 3 skipped; 524 files formatted; ruff clean; mypy — no issues in 5 files | `config.runtime` stores the live container opaquely, so platform configuration no longer imports the composition container even under `TYPE_CHECKING`. The obsolete baseline pair and stale manifest exemption are removed. |
| Move configuration-backed secret handling out of platform common | Complete | `2d33b54` | Full pytest — 2,059 passed, 3 skipped; ruff format/check; mypy — no issues in 5 files | `config.security` now owns secret-file loading, API authentication, and web-session handling; all consumers retain their behavior while `utils` no longer imports `config.settings`. The `platform.common → platform.config` baseline edge is removed. |

## Stage 5 — Split oversized modules

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Extract ledger-backed model attribution from Orchestrator | Complete | `fd19a3b` | Focused pytest — 15 passed; full pytest — 2,061 passed, 3 skipped; ruff format/check; architecture mypy | `orchestrator.model_attribution` owns stable, fail-open ledger model attribution; the existing `Orchestrator._models_used_by` seam delegates to it, preserving callers. |
| Extract model-scarcity classification from Orchestrator | Complete | `a3df595` | Focused pytest — 42 passed; full pytest — 2,061 passed, 3 skipped; ruff format/check; architecture mypy | `orchestrator.model_scarcity` owns the tagged failure type, canonical user message, and all-model-unavailable classifier. Compatibility aliases retain existing `orchestrator.orchestrator` callers. |
| Extract bounded handoff-artifact reading from Orchestrator | Complete | `5fe0b08` | Focused pytest — 5 passed; full pytest — 2,063 passed, 3 skipped; ruff format/check; architecture mypy | `orchestrator.handoff_artifacts` owns fail-open capped artifact reads; the existing private reader name remains imported by Orchestrator for compatibility. |
| Extract primary handoff-artifact path resolution from Orchestrator | Complete | `464126f` | Focused pytest — 6 passed; full pytest — 2,064 passed, 3 skipped; ruff format/check; architecture mypy | `orchestrator.handoff_artifacts` resolves the first declared stage output from its handoff template; the existing Orchestrator method remains the compatibility seam. |
| Extract optional CLI dictation helpers | Complete | `03088a9` | Focused pytest — 5 passed; full pytest — 2,065 passed, 3 skipped; ruff format/check; architecture mypy | `cli.dictation` owns hotkey parsing and in-memory PCM WAV encoding; `cli.main` retains its existing helper names as compatibility aliases. |
| Extract CLI provider environment-file operations | Complete | `58ab44b` | Focused pytest — 9 passed; full pytest — 2,067 passed, 3 skipped; ruff format/check; architecture mypy | `cli.provider_env` owns `.env` parsing, key updates, and process export; `cli.main` retains compatibility aliases. |
| Extract generic provider selection parsing | Complete | `179574c` | Focused pytest — 10 passed; full pytest — 2,068 passed, 3 skipped; ruff format/check; architecture mypy | `cli.provider_env` owns deduplicating 1-based provider selection parsing while `cli.main` retains its provider-list-bound compatibility wrapper. |
| Extract provider configuration detection | Complete | `729ee0c` | Focused pytest — 11 passed; full pytest — 2,069 passed, 3 skipped; ruff format/check; architecture mypy | `cli.provider_env` owns injected OAuth, process-environment, and `.env` provider availability detection; `cli.main` retains lazy registry lookup and compatibility wrappers. |
| Extract CLI cron day selection | Complete | `f6ee24d` | Focused pytest — 35 passed; full pytest — 2,071 passed, 3 skipped; ruff format/check; architecture mypy | `cli.scheduling` owns validator-injected day normalization while `cli.main` retains local Typer error presentation and its compatibility helper. |
| Extract frontend asset staleness detection | Complete | `cec6733` | Focused pytest — 9 passed; full pytest — 2,071 passed, 3 skipped; ruff format/check; architecture mypy | `cli.web_build` owns bundled-asset freshness comparison; npm invocation and CLI messaging stay in `cli.main`, which re-exports the existing helper name. |
| Extract startup-failure log analysis | Complete | `22da021` | Focused pytest — 14 passed; full pytest — 2,071 passed, 3 skipped; ruff format/check; architecture mypy | `cli.startup_report` owns the exception-line pattern and best-effort log tail extraction; `cli.main` keeps failure reporting and its compatibility helper name. |
| Extract update install-spec resolution | Complete | `80a1a79` | Focused pytest — 9 passed; full pytest — 2,073 passed, 3 skipped; ruff format/check; architecture mypy | `cli.update_spec` owns uv git-spec pinning; installation side effects remain in `cli.main`, which retains the existing helper name. |
| Extract agent tool-result interpretation | Complete | `4c35ab2` | Focused pytest — 60 passed; full pytest — 2,076 passed, 3 skipped; ruff format/check; architecture mypy | `agents.tool_results` owns fail-safe success, refusal, unanswered-approval, and delegation-failure reading; `agents.agentic_llm_agent` keeps its existing private names as aliases. |
| Extract fact deduplication normalization | Complete | `1d01293` | Focused pytest — 17 passed; full pytest — 2,079 passed, 3 skipped; ruff format/check; architecture mypy | `memory.dedup` owns filler-word canonicalization for duplicate fact detection; `memory.facts` retains its private helper name as an alias. |
| Extract cockpit artifact path policy | Complete | `db26e31` | Focused pytest — 16 passed; full pytest — 2,083 passed, 3 skipped; ruff format/check; architecture mypy | `web.artifacts` owns permitted roots, state-file exclusion, task attribution, and identifier resolution; `web.api` keeps HTTP mapping. The ownership gate caught the new path and the manifest now declares it. |
| Extract TUI text and slash-input parsing | Complete | `d850617` | Focused pytest — 28 passed; full pytest — 2,087 passed, 3 skipped; ruff format/check; architecture mypy | `cli.tui_text` owns turn rendering, token estimation, and slash-argument/context-document parsing; `cli.tui` retains its private names as aliases. |

## Stage 7 — Evidence-based duplication reduction

Duplication was located mechanically, not by impression: an AST pass hashed every
normalized function body (three or more statements) across the repository and
grouped identical implementations appearing in more than one file. That found
**10 groups**, of which exactly **one was production code**; the rest are test
helpers, where local fixtures are preferable to shared indirection.

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Unify the provider duration parser | Complete | `21ee069` | Focused pytest — 59 passed; full pytest — 2,106 passed, 3 skipped; ruff format/check; architecture mypy | `inference.rate_limit_status` and `inference.providers.openai_compat` held byte-identical protobuf Duration parsers. `inference.durations` now owns it; a test asserts both call sites resolve to the same function object, so the copy cannot silently return. |

One near-duplicate was deliberately left alone: `memory.facts` matches
`SECRET_RE | CC_RE` while `utils.secrets.contains_secret` also matches
`TOKEN_RE`. Unifying them would make the fact store start rejecting content it
currently accepts, which is a behavior change and needs its own decision. Recorded
in [the compatibility-seam register](COMPATIBILITY_SEAMS.md).

**Stage 7 status: Complete.** The one proven production duplicate is gone with
regression coverage; no other cross-file production duplication is detectable by
the scan above.

## Stage 8 — Fitness functions and completion audit

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Make boundaries, ownership, scopes and packaging required CI gates | Complete | `1aa7e37` | CI YAML parsed; architecture pytest — 14 passed; mypy `inference/ architecture/` — no issues in 51 files; full pytest — 2,106 passed, 3 skipped | New `architecture` job fails fast and names the reason; the type gate now also covers `architecture/`. |
| Pin the packaging contract | Complete | `0554f2f` | Architecture pytest — 14 passed; `uv build` succeeded; wheel inspected | `test_packaging.py` asserts the `north` console script and target, package discoverability, `tests*` exclusion, and the `web/node_modules` sdist prune. It also caught that `mcp/` has no `__init__.py` and ships only because namespace discovery is enabled - now guarded. |
| Update contributor and agent guidance | Complete | `1aa7e37` | Reviewed against the enforced tests | `CONTRIBUTING.md` documents each gate, what fails it, and the layer direction. |
| Track every compatibility shim | Complete | `1aa7e37` | Each claim verified by import and object-identity checks | [COMPATIBILITY_SEAMS.md](COMPATIBILITY_SEAMS.md): no temporary exemptions outstanding; every Stage 5 re-export has an owner and removal condition. |

### Final audit

Measured on `1aa7e37`:

| Property | Result |
|---|---|
| Modules declared, each owning its paths exactly once | 18 across 6 layers |
| Manifest `temporary_exemptions` | None remaining |
| Forbidden import edges | 23, down from 27 at baseline; graph matches the checked-in baseline exactly |
| Protected modules requiring explicit authorization to edit | `architecture`, `safety_policy`, `composition.app`, `application.approval`, `integrations.tools` |
| Architecture gate tests | 14, required in CI |
| Full suite | 2,106 passed, 3 skipped |
| Pre-existing non-fatal warnings | 4, unchanged throughout the program |

The 23 remaining edges are declared debt, not exemptions: each is listed in
`architecture/import-baseline.txt`, and the gate fails on any pair not in that
file. Paying one down means deleting its line in the same change.

**Stage 8 status: Complete.**

## Continuing debt paydown (post-Stage 8)

The gate prevents new forbidden pairs; each existing one is paid down by deleting
its line from `architecture/import-baseline.txt` in the change that removes it.

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Invert `platform.config → application.approval` | Complete | `f83e0a9` | Focused pytest — 242 passed (config, approval, architecture); full pytest — 2,106 passed, 3 skipped; ruff format/check; mypy | The approval *mode* is a value the user sets, so it moved to `config/approval_mode.py`; the policy that interprets it stays in `approval/policy.py`. 25 import sites updated, no shim left behind. Platform no longer depends upward on anything outside its own layer. |

### Remaining debt, categorized

22 pairs remain. They are not equivalent problems:

**Intra-layer sibling imports (12).** Flagged because each layer declares the
layers it may import and none lists itself, so any sibling import registers.
These are mostly benign (`platform.ledger → platform.common` is a store using
shared primitives; `intelligence.skills → intelligence.memory` is a real
dependency in the right direction). Paying them down means either declaring an
intra-layer DAG in the manifest or accepting them - a policy decision, not a code
change. Deliberately left for a decision rather than silently loosened, because
the same strictness is what caught the `platform.common → platform.config` cycle.

**Upward cross-layer imports (10).** These are the real architectural debt:

- `integrations.tools → application.{agents,approval,jobs}` and the reverse
  `application.{agents,approval} → integrations.tools` form the remaining
  two-way coupling. The runtime dispatch half of this is already inverted through
  `utils/tools.py` ports; what is left is approval gating and delegation.
- `intelligence.{inference,workspace_context} → integrations.tools` and
  `interfaces.{cli,web} → integrations.tools` are single-direction reads that a
  narrow port would remove.

Each is a Stage-4-shaped unit: define a platform port, inject the concrete type
at the composition root, delete the baseline line.

## Stages 1–8

| Stage | Status | Preconditions | Completion evidence |
|---|---|---|---|
| 1. Architecture map and ownership contracts | Complete | Stage 0 complete | Module map, contracts, manifest schema validation |
| 2. Enforce boundaries and edit permissions | Complete | Stage 1 complete | Import and edit-scope enforcement tests |
| 3. Composition root | Complete | Stage 2 complete | Startup, CLI, API compatibility checks |
| 4. Remove reverse dependencies | Complete | Stage 3 complete | Integration modules no longer import orchestration internals; platform/common has no upward dependencies; forbidden compatibility edges decreased from 27 to 23. |
| 5. Split oversized modules | Complete | Stage 4 complete | Every priority module in the plan now has at least one cohesive extraction with focused tests: orchestrator, cli/main, cli/tui, agentic agent, memory facts, web api. |
| 6. Physical package migration | Not executed (decided) | Stage 5 complete | [ADR 0004](../adr/0004-keep-top-level-packages.md): packages stay at the repository root. The verification this stage required is now permanent in `tests/unit/architecture/test_packaging.py`. |
| 7. Duplication reduction | Complete | Stage 6 decided | An AST duplicate scan found one production duplicate; it is removed with shared-behavior tests (`21ee069`). |
| 8. Fitness functions and completion audit | Complete | Stage 7 complete | Architecture and packaging gates are required in CI (`1aa7e37`); guidance, shim register, and final audit recorded. |

## Baseline Failures

None recorded yet. This section will list only failures that existed before the relevant refactor change.

## Active Blockers

None.
