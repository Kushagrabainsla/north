---
name: deep-module-architecture
description: "Use when designing or evaluating module boundaries to ensure deep abstractions (high capability behind minimal interfaces), eliminating pass-through boilerplate and leaky seams."
domains:
  - engineering
---
# Deep Module Architecture

> **The best modules are deep: they hide high complexity and extensive behavior behind a simple, intuitive interface.** (John Ousterhout, *A Philosophy of Software Design*)

## Use this when
- Designing new packages, classes, subsystems, or tool interfaces.
- Refactoring sprawling codebases where changes cause cascading ripple effects.
- Evaluating architectural health to identify shallow abstractions and pass-through layers.

## Do NOT use for
- Pure syntax cleanup or dead code removal (use `code-simplification`).
- Safe refactoring of single functions without boundary redesign (use `safe-refactoring`).

---

## Core Principles & Vocabulary

* **Module**: A distinct unit of code with an interface and an implementation (function, class, module, or package).
* **Interface**: Everything a caller must understand to use the module correctly (types, invariants, error behaviors, ordering constraints).
* **Depth**: The ratio of internal capability to interface complexity.
  - **Deep Module**: Small surface area, rich internal leverage. Example: standard library file I/O or garbage collection.
  - **Shallow Module**: Interface is almost as complex as the implementation. Example: getters/setters, thin pass-through wrappers.
* **Seam**: A clean place where behavior can be altered or intercepted without editing call sites.
* **Leverage**: Capability delivered to callers per unit of interface complexity they must learn.
* **Locality**: Concentration of related logic so bugs, verification, and invariants live in one place.

---

## The Deletion Test
When evaluating a suspected shallow module, ask:
> *"If this module were deleted or merged into its consumer/provider, would complexity concentrate into a clearer abstraction, or merely move around?"*
If deleting it removes useless indirection without losing leverage, the module was shallow.

---

## Procedure

### 1. Survey the Architectural Hotspots
Review the files and modules with the highest change velocity and bug frequency:
- Run `git log --oneline -- <path>` to find churn hotspots.
- Identify friction points where understanding one feature requires hopping between 5+ small files.

### 2. Diagnose Shallow Modules and Leaky Seams
Audit candidates against common architectural smells:
- **Pass-through methods**: Method `A()` merely calls `B()` with the same arguments and minimal logic.
- **Leaky abstractions**: Callers must know internal state machine details or call methods in an exact undocumented order.
- **Artificial pure extractions**: Functions extracted solely for unit-test mockability that destroy locality and let integration bugs hide in orchestration.
- **Configuration sprawl**: Forcing callers to pass 10 configuration flags instead of choosing sensible defaults internally.

### 3. Define Deep Seams and Interfaces
Design the replacement interface:
- Minimize the number of public methods and required parameters.
- Handle common cases automatically (e.g. self-healing connections, auto-retries, sensible defaults).
- Pull complexity *into* the implementation rather than pushing it outward onto callers.

### 4. Execute the Consolidation
Apply the refactoring:
- Consolidate shallow intermediaries into the core deep module.
- Ensure the interface acts as the primary test surface (test through the interface, not internal private helpers).
- Verify that call sites become shorter, cleaner, and decoupled from implementation mechanics.

### 5. Verify Invariants and Test Leverage
Run test suites and type checkers:
```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check .
```

---

## Red Flags
- Classes with 15 one-line pass-through methods.
- "Manager", "Helper", or "Handler" classes that contain no state and merely delegate.
- Unit tests that mock 8 internal collaborators just to test a 3-line function.
- Changing one private data structure requires editing 10 different files.

## Verification Checklist
- [ ] Interface is substantially simpler than the behavior it encapsulates
- [ ] Passed the Deletion Test for all candidate abstractions
- [ ] Tests exercise behavior through the deep interface rather than mocked internals
- [ ] No pass-through or boilerplate proxy methods remain
