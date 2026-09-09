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
| Exclude vendored frontend dependencies from sdist | Ready to commit | Pending | Baseline artifact inspection: sdist had 717 `web/node_modules` entries; wheel had 0 | `MANIFEST.in` plus a CI tarball-content assertion prevent the regression. |
| Capture backend/frontend/packaging baseline | In progress | — | Backend: ruff format/check pass; pytest — 1,956 passed, 3 skipped. Frontend: 9 tests passed; production build passed. Packaging: `uv build` succeeded. | CLI help and Docker Compose syntax passed; Compose was run once with the local credential rendered and will be sanitized in future checks. |
| Add ADR index and baseline decision records | Not started | — | — | No architecture implementation moves before this control plane is complete. |

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
