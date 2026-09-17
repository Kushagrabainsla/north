---
name: test-design-and-regression-coverage
description: "Use when adding or expanding tests, or fixing a bug that needs a regression test. Design a test matrix and write deterministic, behaviour-focused tests."
---
# Test design and regression coverage

> **When fixing a bug, write the failing test first; ship no fix a test does not lock in.**

## Use this when
- You are adding tests, expanding coverage, or a bug fix needs a regression test.

## Do NOT use for
- Non-test code changes (production logic).

## Procedure
1. Build a test matrix: normal case, empty input, boundary value, invalid/None input, and the prior-bug/regression row.
2. For a bug fix, write the regression test FIRST and watch it fail, then fix.
3. Assert on observable behaviour, not implementation detail.
4. Keep tests deterministic: no real clock, network, or order dependence; control randomness.
5. One concept per test; mock only true external boundaries, never the code under test.
6. Implement only the matrix rows this change actually needs.

## Done when
- The new/failing behaviour is covered, tests are deterministic, and they fail if the behaviour regresses.
