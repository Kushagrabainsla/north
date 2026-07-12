---
name: verifying-a-north-change
description: "Use before declaring any north code change done - to confirm tests and linting pass and nothing regressed."
---
# Verifying a change to north

> **A change is not done until it is proven green - never on the model's word alone.**

## Use this when
- You edited north's Python and are about to report the task done.

## Do NOT use for
- Docs-only changes, or work still in progress.

## Procedure
1. Run the focused tests for what you changed: `.venv/bin/python -m pytest -q tests/unit/<area>`. A pass prints `N passed`.
2. Lint: `.venv/bin/ruff check .` (line length is 120). Fix every issue and re-run until it prints `All checks passed!`.
3. If you touched shared code (models, registry, orchestrator, base classes), run the full suite: `.venv/bin/python -m pytest -q`.
4. Never weaken a test to make it pass; fix the code, or update an assertion only when behaviour legitimately changed.
5. State the evidence in your final answer - exactly what you ran and the result.

## Done when
- Focused tests + lint are green (plus the full suite if shared code changed), and the evidence is stated.
