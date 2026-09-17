---
name: concurrency-and-async-correctness
description: "Use when writing or fixing concurrent or async code - threads, async/await, tasks, locks, retries, cancellation, timeouts, or shared mutable state."
---
# Concurrency and async correctness

> **Assume every interleaving happens; shared mutable state without a guard is a race.**

## Use this when
- The change involves threads, asyncio tasks, locks, background jobs, retries, cancellation, or timeouts.

## Do NOT use for
- Single-threaded, synchronous logic.

## Procedure
1. Identify shared state and who writes it. Make it immutable, or guard it with a lock/queue.
2. Avoid mutable default arguments and module-global accumulation across calls.
3. For every `await` / blocking call, define its timeout and its cancellation behaviour.
4. Release resources on every path (use context managers / `finally`).
5. Make retries idempotent.
6. Test the contended, cancelled, and timed-out paths - not just the sequential one.

## Done when
- Shared state is guarded, cancellation/timeouts are defined, and a concurrency-stressing test passes.
