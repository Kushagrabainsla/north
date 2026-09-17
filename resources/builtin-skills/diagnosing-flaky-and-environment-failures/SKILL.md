---
name: diagnosing-flaky-and-environment-failures
description: "Use when a failure is intermittent, happens only in CI, is order-dependent, or looks like a tooling / PATH / venv / dependency problem rather than a logic bug."
---
# Diagnosing flaky and environment failures

> **Prove it is the code before you "fix" the code.**

## Use this when
- A test passes sometimes and fails others, fails only in CI, or the error smells like setup (command not found, import error, version mismatch).

## Do NOT use for
- A deterministic, always-reproducible logic bug (use `systematic-debugging`).

## Procedure
1. Reproduce in isolation: run the one failing test alone, then with the full suite, to expose order dependence and shared state.
2. Check the exit code: 127/126 mean the command is missing or not executable - not a test failure.
3. Check the interpreter, virtualenv, PATH, and installed versions actually used at runtime.
4. Look for nondeterminism: time, randomness, network, filesystem, global/shared mutable state, unordered collections.
5. Only after this, decide code vs environment, and fix the layer that is actually wrong.

## Done when
- The failure is reliably reproducible or explained as environment, and the real layer is fixed (not masked by a retry).
