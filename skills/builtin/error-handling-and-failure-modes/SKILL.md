---
name: error-handling-and-failure-modes
description: "Use when adding a call that can fail - file IO, subprocess, network or API calls, parsing, or external tools - to handle its failure paths, not only the happy path."
---
# Error handling and failure modes

> **Every external call has a failure path; write it before you ship the happy path.**

## Use this when
- You are adding IO, a subprocess, a network/API request, deserialization, or any call that can fail or return junk.

## Do NOT use for
- Pure in-memory logic with no external boundary.

## Procedure
1. List the failure modes for the call: timeout, not-found, permission denied, malformed/partial data, rate-limit, empty result.
2. Classify each as retryable or fatal.
3. Decide the behaviour: retry with backoff, fall back, or surface a clear error that keeps the original cause (`raise ... from exc`).
4. Never swallow errors silently and never `except:` bare.
5. Add a test that exercises at least one failure path.

## Done when
- The unhappy paths are handled explicitly and at least one is tested.
