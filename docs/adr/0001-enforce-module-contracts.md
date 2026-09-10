# ADR 0001: Enforce Module Contracts and Self-Edit Scopes

- **Status:** Accepted
- **Date:** 2026-09-09

## Context

North is intended to improve its own codebase. Today, functional directories exist, but their ownership, allowed dependencies, mutable paths, and required validations are not represented in one enforceable contract. Folder placement and agent prompt instructions alone cannot reliably prevent a self-editing agent from modifying the wrong module or adding a reverse dependency.

## Decision

Introduce a version-controlled module manifest at `architecture/modules.yaml`. Each module will declare its purpose, owner, allowed imports, public contracts, agents that may modify it, editable paths, and required validation commands.

The manifest will be consumed by three enforcement points:

1. planning assigns explicit module/path scope to a coding task;
2. source-mutating tools reject edits outside that scope and report the rule that denied them;
3. AST-based architecture tests reject imports forbidden by the module dependency graph.

Cross-module changes require an explicit declared scope and the union of all affected validation requirements. Emergency exceptions must be explicit, user-approved, and auditable.

## Consequences

- Agents gain clear, enforceable edit permissions rather than broad implicit access.
- Developers gain a discoverable ownership and validation map.
- Architecture changes require maintaining the manifest and boundary tests.
- The initial manifest is documentation and validation infrastructure; it does not change user-facing North behavior.

## Compatibility and Validation

No CLI, API, configuration, persistence, or plugin behavior changes in this decision record. The implementation must prove both that forbidden edits/imports fail and that valid in-scope and authorized cross-module changes continue to work.
