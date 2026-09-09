# ADR 0003: Compatibility-First, Staged Architecture Migration

- **Status:** Accepted
- **Date:** 2026-09-09

## Context

North is a live application with public CLI commands, HTTP APIs, provider configuration, persistent SQLite data, agent/tool discovery, Docker workflows, and a React frontend. A repository-wide `src/north` move or broad folder consolidation would create a large diff and obscure behavior regressions before module boundaries are enforceable.

## Decision

Refactor in small validated stages. Establish architecture contracts and enforcement first; extract the composition root and remove reverse dependencies next; split oversized modules; then migrate physical packages one dependency layer at a time. Retain compatibility adapters for public or plugin-facing import paths during documented migration windows.

Every logical unit must be independently testable, committed, and pushed only after targeted and applicable broad checks pass. Newly discovered defects are repaired separately with regression coverage.

## Consequences

- The program takes longer than a one-shot move but is reviewable and reversible.
- `src/north` is a target state, not an immediate prerequisite.
- Compatibility tests and data-migration fixtures become required before changing paths or schemas.
- The status ledger records evidence, baseline issues, and blockers throughout execution.

## Compatibility and Validation

All existing CLI, API, config/env, persistent-data, and plugin/discovery behavior is preserved unless the user explicitly approves a separate breaking change. Each migration must demonstrate installation, startup, public-interface, persistence, frontend, and deployment compatibility appropriate to its scope.
