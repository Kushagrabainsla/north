# Clean Architecture Refactor Program

**Status:** Approved for staged execution  
**Branch:** `refactor/clean-architecture`  
**Scope:** Entire repository  
**Compatibility policy:** Preserve all existing CLI, API, configuration, persisted-data, and plugin/discovery behavior.

## Purpose

North will evolve from a flat set of broadly coupled top-level packages into a clean, enforceable architecture. The outcome is not merely fewer folders: it is a system in which developers and self-editing agents can determine which module owns a concern, what it may depend on, what paths it may modify, and which validation is required before a change is accepted.

Architecture is a behavioral and security concern. Folder moves do not by themselves improve runtime reliability. Dependency direction, explicit ports, module contracts, edit enforcement, compatibility coverage, and regression tests do.

## Operating Contract

1. Work is performed as one small, cohesive logical change at a time.
2. Each change has a declared module scope, an explicit compatibility expectation, and required validation.
3. Targeted validation runs first, followed by every applicable broader quality gate.
4. A change is committed and pushed only after its gates pass and its diff is inspected.
5. A stage advances only after all of its changes are complete and validated.
6. A discovered defect is fixed in an isolated change with a regression test; it is never hidden in an unrelated structural refactor.
7. Known baseline failures are documented separately and never accepted as new regressions.
8. Work pauses only for an unavoidable breaking-change decision, persistent validation failure, security/data-migration risk, or a new dependency/configuration decision.

## Regression Policy

No unit is done without concrete evidence that it has not regressed applicable behavior:

- CLI commands, flags, output-critical behavior, startup, update, and authentication flows;
- HTTP routes, payloads, status codes, SSE/event behavior, and web session security;
- existing `NORTH_*` environment variables, settings files, defaults, and secret handling;
- existing SQLite schemas and user state under `~/.north/`, including safe migrations;
- agent discovery, tool discovery, approval gates, tool schemas, and autonomy behavior;
- frontend build and relevant frontend/API behavior;
- installer, Docker, and CI workflows.

Before moving uncovered behavior, add characterization tests. If validation exposes a regression, stop progression, fix the regression, rerun all affected checks, and record the result before continuing.

## Target Architecture

The eventual layout is intentionally deferred until dependency boundaries are stable:

```text
src/north/
  app/                    # composition root: startup, lifecycle, concrete wiring
  application/            # task workflows and use cases
    orchestrator/
    agents/
    approval/
    scheduling/
    bootstrap/
  intelligence/           # inference and learned/user/workspace knowledge
    inference/
    memory/
    context/
    skills/
  platform/               # settings, persistence, dependency-light common code
    config/
    persistence/
    common/
  integrations/           # external capabilities and systems
    tools/
    gateways/
    mcp/
  interfaces/             # CLI, HTTP, Telegram adapters
    cli/
    http/
    telegram/
frontend/                 # React/Vite application only
```

The current modules are not to be merged into a monolith. They will be placed beneath these architectural groups after their contracts are proven.

## Dependency Direction

```text
interfaces ─────┐
integrations ───┼──> application ──> intelligence / platform
app ────────────┘          │
                            └──> integrations through injected ports only
```

Rules:

- `app` is the only composition root and the only layer that constructs concrete services.
- `platform` does not import application, interface, or integration layers.
- `intelligence` may use platform but does not import application or interfaces.
- `application` defines ports and workflows; it does not construct infrastructure.
- `integrations` implement application ports and must not import orchestration internals.
- `interfaces` adapt user/network input into application calls and do not contain business workflow logic.
- `tools` must not import `orchestrator`.
- configuration contains settings/value types, not production dependency wiring.

## Self-Editing Agent Boundaries

A version-controlled `architecture/modules.yaml` will define each module's owner, allowed imports, editable agent roles, editable paths, required validation, and public contracts.

This manifest will be enforced, not merely documented:

1. Plans declare the modules and paths an agent may modify.
2. Source-mutating tools reject out-of-scope edits.
3. Cross-module edits require an explicit architecture-approved scope and all affected validations.
4. AST-based import-boundary tests reject forbidden dependencies.
5. Every refusal explains which contract blocked the action.
6. Any emergency override is explicit, user-approved, and auditable.

## Stages

### Stage 0 — Safety baseline and control plane

Create this durable plan and a status ledger; establish the branch and baseline tests; document existing failures; add ADR tracking. No behavior changes.

**Exit criteria:** baseline command results are recorded; refactor status is durable; working tree is clean; the control-plane change is committed and pushed.

### Stage 1 — Architecture map and ownership contracts

Correct the repository map, document every production module's responsibility and public surface, add `architecture/modules.yaml`, and record ADRs for dependency direction, composition-root-only wiring, self-edit permissions, and compatibility migration.

**Exit criteria:** every production module has ownership, allowed dependencies, editable roles/paths, and required validation.

### Stage 2 — Enforce boundaries and edit permissions

Implement manifest loading/validation, AST import-boundary tests, task edit scopes, source-mutation enforcement, clear refusal behavior, and auditable cross-module change flow.

**Exit criteria:** forbidden imports fail checks; agents cannot edit files outside scope; valid cross-module work remains possible.

### Stage 3 — Establish a composition root

Extract startup, lifespan, FastAPI assembly, and concrete dependency construction from `orchestrator/app.py` and `config/dependencies.py` into `app`. Preserve all current entry points via compatibility adapters.

**Exit criteria:** orchestration contains workflow logic rather than system bootstrapping; configuration no longer owns production wiring; only the composition root assembles concrete implementations.

### Stage 4 — Remove reverse dependencies and shrink hubs

Remove `tools -> orchestrator` and comparable reverse dependencies using narrow injected protocols. Restrict `utils` to pure dependency-light code, moving configuration-, HTTP-, and application-aware utilities to their owning modules.

**Exit criteria:** integration modules do not import orchestration internals; platform/common has no upward dependencies; documented cycles decrease.

### Stage 5 — Split oversized modules

Split the largest files by cohesive responsibility, in this priority order:

1. `orchestrator/orchestrator.py`
2. `cli/main.py`
3. `cli/tui.py`
4. `agents/agentic_llm_agent.py`
5. `memory/facts.py`
6. `web/api.py` and `web/src/pages/Verbose.tsx`

Use small focused extractions, preserve public imports during migration, and never combine behavior changes with mechanical moves.

**Exit criteria:** each extracted unit has a single clear reason to change and corresponding focused tests.

### Stage 6 — Migrate physical packages

Only after prior stages are stable, introduce `src/north/` and migrate groups in dependency order: platform, intelligence, integrations, application, interfaces, then frontend/build paths. Maintain temporary compatibility modules where a public or plugin-facing import needs one.

**Exit criteria:** clean installation, CLI name, API behavior, persistent data, Docker, installer, CI, and frontend build remain compatible.

### Stage 7 — Evidence-based duplication reduction

Remove duplication only where two or more clients share truly identical policy and lifecycle. Prefer small protocols and composition over broad inheritance or generic helpers.

**Exit criteria:** each new abstraction has multiple legitimate clients; behavior/security coverage stays intact.

### Stage 8 — Permanent fitness functions and completion audit

Make module-boundary, manifest, installation, backend, frontend, and compatibility checks required CI gates. Update contributor and agent guidance. Track all temporary shims with an owner and removal version.

**Exit criteria:** both humans and agents can reliably determine module ownership, edit scope, dependency rules, and validation; CI prevents architectural regressions.

## Per-Change Gate

1. Confirm the module contract and permitted path scope.
2. Read the relevant implementation and current tests.
3. Add or update characterization/regression coverage as needed.
4. Make one cohesive change.
5. Run targeted tests.
6. Run applicable lint, formatting, type, build, and compatibility checks.
7. Inspect the diff for unrelated/generated files.
8. Record evidence in `docs/refactor/STATUS.md`.
9. Commit explicit files only.
10. Push the validated commit to `origin/refactor/clean-architecture`.

## Validation Baseline

The exact command set is recorded in the Stage 0 status entry. Expected checks include, when applicable:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run mypy ...
npm test --prefix frontend
npm run build --prefix frontend
docker build ...
north --help
north start --no-chat
```

No command is treated as passing until it is verified against this repository's actual configuration and baseline.
