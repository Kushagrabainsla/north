---
name: systematic-debugging
description: "Use when a reproducible bug, error, or test failure has an unknown cause and you are about to fix it. Find the root cause before changing any code."
---
# Systematic debugging

> **No fix without a reproduced failure and an identified root cause.**

## Use this when
- A test fails, an exception is thrown, or behaviour is wrong, and you don't yet know why.

## Do NOT use for
- Behaviour that changed between a known-good and known-bad revision (use `finding-a-regression`).
- Intermittent / CI-only / order-dependent failures (use `diagnosing-flaky-and-environment-failures`).

## Procedure
1. Write down the exact repro command, the observed result, the expected result, and the smallest code path you suspect.
2. Reproduce it. If you cannot reproduce it, you cannot fix it yet.
3. Read the full error and stack trace to the deepest frame in the code you can change. The message often names the cause.
4. Form ONE hypothesis. Confirm it with a probe (a log line, assertion, or breakpoint) BEFORE editing.
5. Fix the root cause, not the symptom.
6. Re-run: watch it go red -> green. Leave the reproduction behind as a regression test.

## Done when
- The failure reproduces no more, a regression test locks it in, and you can name the root cause in one sentence.
