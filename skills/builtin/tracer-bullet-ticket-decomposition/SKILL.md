---
name: tracer-bullet-ticket-decomposition
description: "Use when decomposing large initiatives or multi-layer features into tracer-bullet vertical slices with explicit blocking DAG edges and expand-contract refactoring sequences."
domains:
  - engineering
---
# Tracer-Bullet Ticket Decomposition

> **Build end-to-end vertical slices that cut through every architectural layer early, rather than building complete horizontal layers in isolation.**

## Use this when
- Planning a non-trivial feature that spans database schema, business logic, API, and UI.
- Structuring multi-step engineering tasks for parallel or subagent execution.
- Managing wide-blast-radius refactors across large codebases.

## Do NOT use for
- Single-line bug fixes or localized tweaks (use `incremental-implementation`).
- High-level project roadmapping without concrete implementation steps (use `alignment-and-grilling`).

---

## Core Principles

### 1. Vertical Slices over Horizontal Layers
* **Horizontal Slicing (Bad)**:
  - Step 1: Write all database models.
  - Step 2: Write all backend services.
  - Step 3: Write all API endpoints.
  - Step 4: Write all frontend UI.
  *Problem*: Nothing is testable or verifiable end-to-end until the very end. Architectural mistakes are discovered too late.
* **Vertical Tracer Bullets (Good)**:
  - Step 1: Minimum viable schema + endpoint + CLI command for *one single action* end-to-end.
  - Step 2: Second user flow end-to-end.
  *Advantage*: Immediate working verification at every stage.

### 2. Explicit Blocking DAG (Directed Acyclic Graph)
Every slice or ticket explicitly lists:
- `Blocks`: Tickets that cannot start until this completes.
- `Blocked By`: Prerequisites that must be finished and green first.
- *Unblocked tickets can immediately run in parallel.*

### 3. Expand-Contract for Wide Blast Radius
When a refactoring touches shared types or signatures used across dozens of files:
1. **Expand**: Add the new interface/method alongside the legacy one without breaking existing callers.
2. **Migrate**: Transition call sites in small, isolated, green batches.
3. **Contract**: Remove the legacy interface once all call sites are migrated.

---

## Procedure

### 1. Identify the Core End-to-End Tracer Path
Pinpoint the simplest full path through the system:
- Minimal input -> minimal processing -> minimal storage -> minimal output.
- This serves as Ticket #1 (the foundational tracer bullet).

### 2. Define Vertical Slices with Strict Context Sizing
Break remaining requirements into discrete slices where each:
- Cuts through necessary layers (schema -> logic -> API/tool -> UI/output -> test).
- Is independently testable and demoable.
- Fits within a single fresh LLM context window (~200-500 lines of modified code).

### 3. Assign Blocking Dependencies
Structure tickets into a dependency graph:
```markdown
- [ ] **Ticket 01 (Tracer Bullet)**: Core schema and minimal end-to-end execution. `Blocks: [02, 03]`
- [ ] **Ticket 02 (Feature A)**: Expand flow A with error handling and unit tests. `Blocked by: [01]`
- [ ] **Ticket 03 (Feature B)**: Expand flow B with background scheduling. `Blocked by: [01]`
- [ ] **Ticket 04 (Integration & UI)**: CLI/UI wiring and end-to-end validation. `Blocked by: [02, 03]`
```

### 4. Sequence Wide Refactorings (Expand-Contract)
For cross-cutting changes:
- Create separate tickets for (a) adding new APIs, (b) migrating caller batches, and (c) deprecating/deleting old code.
- Ensure the test suite passes on every intermediate commit.

### 5. Execute and Track
- Pick unblocked tickets.
- Implement each slice, verify with automated tests, and mark complete before proceeding to dependents.

---

## Red Flags
- Horizontal tickets like "Create all DTOs" or "Write all database migrations" with no end-to-end verification.
- Giant tickets that require modifying 30+ files simultaneously without an expand-contract phase.
- Circular dependencies in ticket graphs.
- Committing broken states with "will fix in next ticket".

## Verification Checklist
- [ ] Ticket 1 implements a working vertical slice end-to-end
- [ ] Each ticket declares explicit `Blocked By` and `Blocks` relationships
- [ ] Wide refactors are decoupled into Expand, Migrate, and Contract phases
- [ ] Each slice is independently verifiable with tests
