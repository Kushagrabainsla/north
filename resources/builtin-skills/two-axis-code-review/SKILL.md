---
name: two-axis-code-review
description: "Use when reviewing a code diff, PR, or implementation against two isolated parallel axes: standards/code hygiene and specification/intent fidelity."
domains:
  - engineering
---
# Two-Axis Code Review

> **Review code along two independent axes: Code Standards (hygiene, smells, performance, safety) and Specification Fidelity (did we build what was asked, and only what was asked?).**

## Use this when
- Reviewing a pull request, git diff, or newly implemented feature before merging.
- Performing pre-commit code audits on complex changes.
- Validating whether an implementation matches both engineering guidelines and user requirements.

## Do NOT use for
- Addressing existing review comments left by others (use `addressing-pr-review-feedback`).
- Automated linting or formatting that tools like `ruff` or `mypy` handle automatically.

---

## Core Philosophy: The Two Independent Axes

Evaluating code along a single blended pass often causes one axis to dominate:
- *Reviewers focus on stylistic nitpicks and miss fundamental logic flaws.*
- *Reviewers verify the feature works but overlook catastrophic security, concurrency, or performance regressions.*

By separating the review into **two isolated evaluations**, neither pollutes the other:

```
                      ┌──────────────────────────────────────┐
                      │            CODE DIFF / PR            │
                      └──────────────────┬───────────────────┘
                                         │
                   ┌─────────────────────┴─────────────────────┐
                   ▼                                           ▼
      ┌─────────────────────────┐                 ┌─────────────────────────┐
      │  AXIS 1: STANDARDS &    │                 │   AXIS 2: SPECIFICATION  │
      │        HYGIENE          │                 │        FIDELITY         │
      │  • Fowler Code Smells   │                 │  • Acceptance Criteria  │
      │  • Safety & Errors      │                 │  • Edge Cases & Scenarios│
      │  • Performance/Leaks    │                 │  • Scope Creep / Drift  │
      └────────────┬────────────┘                 └────────────┬────────────┘
                   │                                           │
                   └─────────────────────┬─────────────────────┘
                                         ▼
                               ┌───────────────────┐
                               │  SYNTHESIS REPORT │
                               │  • Blockers       │
                               │  • Suggestions    │
                               │  • Verdict        │
                               └───────────────────┘
```

---

## Procedure

### 1. Collect Diff Context and Original Intent
- Gather the git diff: `git diff main...HEAD` or `git diff HEAD~1`.
- Gather the originating issue, user prompt, spec, or acceptance criteria.

### 2. Axis 1 Evaluation: Standards & Hygiene
Evaluate the implementation purely against code quality fundamentals:
- **Code Smells (Fowler)**: Dead code, feature envy, excessive parameters, duplicated logic, shallow abstractions.
- **Safety & Error Handling**: Are exceptions caught specifically? Are resources cleaned up via context managers (`with`, `async with`)?
- **Type Correctness & Style**: Do type annotations accurately reflect runtime shapes?
- **Performance & Resource Leaks**: Are there unindexed queries, unbounded in-memory accumulations, or missing connection timeouts?
- **Concurrency**: Are shared resources protected? Any race conditions in async code?

### 3. Axis 2 Evaluation: Specification & Intent Fidelity
Evaluate the implementation purely against the requested feature:
- **Acceptance Criteria**: Does the change implement every agreed requirement?
- **Edge Cases & Regressions**: What happens on empty inputs, network timeouts, invalid payloads, or boundary values?
- **Unintended Behavior / Scope Creep**: Did the PR add unrequested features or modify unrelated files?
- **Test Coverage**: Do unit/integration tests actually assert the business invariants?

### 4. Synthesize the Review Report
Format findings into clear severity tiers:

```markdown
### 🛡️ Code Review Synthesis

#### 🚨 Blockers (Must Fix Before Merge)
1. **[File:Line]**: [Description of critical bug, security vulnerability, or missed acceptance criteria]

#### 💡 Suggestions (Improvements & Hygiene)
1. **[File:Line]**: [Opportunity for deeper module abstraction or readability enhancement]

#### ✨ Positive Observations
- [Noteworthy well-designed abstractions, good test coverage, or clean patterns]

#### Verdict: [APPROVED | CHANGES REQUESTED]
```

### 5. Verify Verification Steps
Ensure automated tests and linter checks run cleanly:
```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check .
```

---

## Red Flags
- Giving an "LGTM" without reading tests.
- Complaining about styling when critical edge cases are completely unhandled.
- Rubber-stamping PRs that silently change behavior in unrelated modules.
- Approving changes with skipped or flaky tests.

## Verification Checklist
- [ ] Axis 1 (Standards, safety, error handling, smells) verified
- [ ] Axis 2 (Spec fidelity, acceptance criteria, regression protection) verified
- [ ] Review report separates blockers from non-blocking suggestions
- [ ] Automated test suite and linters pass cleanly
