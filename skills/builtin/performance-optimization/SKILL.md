---
name: performance-optimization
description: "Use when asked to make something faster or use less memory, and its correctness is already established."
---
# Performance optimization

> **Measure first; never optimize on a guess.**

## Use this when
- There is a concrete speed/memory goal and the code is already correct.

## Do NOT use for
- Code that is not yet correct, or premature optimization with no measured problem.

## Procedure
1. Define the metric and target (latency, throughput, memory) and a representative workload.
2. Measure the baseline; profile to find the ONE real hotspot.
3. Optimize that hotspot - prefer algorithmic wins (big-O, fewer calls, batching) over micro-tuning.
4. Re-measure against the baseline; keep the change only if it moved the metric.
5. Confirm behaviour and tests are unchanged.

## Done when
- The target is met, proven by before/after numbers, with behaviour intact.
