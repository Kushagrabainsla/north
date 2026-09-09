# Clean Architecture Refactor Status

This ledger is the durable execution record for [the approved refactor program](CLEAN_ARCHITECTURE_PLAN.md). It is updated in the same change as each completed logical unit.

## Rules

- Do not mark a unit complete without command output or other concrete validation evidence.
- Record pre-existing failures separately from regressions.
- Do not advance a stage while a regression or unresolved blocker remains.
- Each completed logical unit must name the commit pushed to `origin/refactor/clean-architecture`.

## Stage 0 — Safety baseline and control plane

| Unit | Status | Commit | Validation evidence | Notes |
|---|---|---|---|---|
| Persist approved plan and status ledger | Complete | `a265963` | `git diff --check`; `uv run ruff check .`; `uv run pytest` — 1,956 passed, 3 skipped | The initial format check exposed two pre-existing test formatting discrepancies; the isolated repair is recorded below. |
| Repair pre-existing formatter violations | Complete | `2a26984` | `uv run ruff format --check .`; `uv run ruff check .`; targeted pytest — 13 passed; full pytest — 1,956 passed, 3 skipped | Behavior-preserving ruff formatting only. |
| Exclude vendored frontend dependencies from sdist | Complete | `aaa1aab` | `uv build`; sdist inspection — 0 `web/node_modules` entries; wheel archive integrity check; `uv run pytest` — 1,956 passed, 3 skipped | The sdist shrank from 4,288,297 to 1,013,863 bytes. CI now asserts the exclusion. |
| Capture backend/frontend/packaging baseline | Complete | `aaa1aab` | Backend: ruff format/check pass; pytest — 1,956 passed, 3 skipped. Inference mypy — no issues in 45 files. Frontend: 9 tests passed; production build passed. Packaging: `uv build` passed. CLI help and sanitized Docker Compose syntax passed. | Existing non-fatal setuptools warning: TypeScript `web/src` is detected as a package-data directory. Address during the frontend/package-layout migration, not in this focused baseline. |
| Add ADR index and baseline decision records | Ready to commit | Pending | ADRs 0001–0003 define enforceable module scopes, composition-root-only wiring, and compatibility-first staged migration. | No architecture implementation moves before this control plane is complete. |

## Stages 1–8

| Stage | Status | Preconditions | Completion evidence |
|---|---|---|---|
| 1. Architecture map and ownership contracts | Not started | Stage 0 complete | Module map, contracts, manifest schema validation |
| 2. Enforce boundaries and edit permissions | Not started | Stage 1 complete | Import and edit-scope enforcement tests |
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
