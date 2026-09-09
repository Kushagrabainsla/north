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
| Enforce task edit scopes in source-mutating tools | Not started | — | — | Must fail closed outside an approved module/path scope. |

## Stages 1–8

| Stage | Status | Preconditions | Completion evidence |
|---|---|---|---|
| 1. Architecture map and ownership contracts | Complete | Stage 0 complete | Module map, contracts, manifest schema validation |
| 2. Enforce boundaries and edit permissions | In progress | Stage 1 complete | Import and edit-scope enforcement tests |
| 3. Composition root | Not started | Stage 2 complete | Startup, CLI, API compatibility checks |
| 4. Remove reverse dependencies | Not started | Stage 3 complete | Boundary checks and cycle report |
| 5. Split oversized modules | Not started | Stage 4 complete | Focused extraction and regression tests |
| 6. Physical package migration | Not started | Stage 5 complete | Install, persistence, API, CLI, Docker, frontend checks |
| 7. Duplication reduction | Not started | Stage 6 complete | Shared-behavior regression coverage |
| 8. Fitness functions and completion audit | Not started | Stage 7 complete | Required CI architecture gates |

## Baseline Failures

None recorded yet. This section will list only failures that existed before the relevant refactor change.

## Active Blockers

None.
