---
name: performance-optimization
description: "Use when asked to make something faster or use less memory, and its correctness is already established."
---
# Performance Optimization

> **Measure first; never optimize on a guess.**

## Use this when
- There is a concrete speed/memory goal and the code is already correct.

## Do NOT use for
- Code that is not yet correct, or premature optimization with no measured problem.

## The Optimization Workflow

```
1. MEASURE → Establish baseline with real data
2. IDENTIFY → Find the actual bottleneck (not assumed)
3. FIX → Address the specific bottleneck
4. VERIFY → Measure again; keep or revert
5. GUARD → Add monitoring or tests to prevent regression
```

## Procedure

### Step 1: Measure the Baseline
Define the metric and target (latency, throughput, memory) and a representative workload. Profile to find the ONE real hotspot.

```bash
# Python profiling
python -m cProfile -s cumulative your_script.py

# Line-level profiling
pip install line_profiler
 kernprof -l -v your_script.py

# Memory profiling
pip install memory_profiler
 python -m memory_profiler your_script.py

# Simple timing
import time
start = time.perf_counter()
result = your_function()
elapsed = time.perf_counter() - start
print(f"Elapsed: {elapsed:.3f}s")
```

### Step 2: Identify the Bottleneck

| Symptom | Likely Cause | Investigation |
|---------|-------------|---------------|
| Slow I/O | Synchronous blocking, missing caching | Profile I/O calls, check for batching opportunities |
| High memory | Large data structures, leaks, unbounded caches | Heap snapshot, `tracemalloc`, check for circular refs |
| CPU-bound | Algorithmic inefficiency, regex backtracking | `cProfile`, check big-O of hot paths |
| Slow startup | Heavy imports, eager initialization | Lazy imports, check import-time side effects |
| Slow queries | N+1 patterns, missing indexes | Query logging, `EXPLAIN ANALYZE` |

### Step 3: Fix Common Anti-Patterns

**N+1 Queries (most common backend bottleneck):**
```python
# BAD: N+1 — one query per task
tasks = db.tasks.find_all()
for task in tasks:
    task.owner = db.users.find(task.owner_id)

# GOOD: Single query with join
tasks = db.tasks.find_all(include=["owner"])
```

**Unbounded Data Fetching:**
```python
# BAD: Fetching all records
all_tasks = db.tasks.find_all()

# GOOD: Paginated with limits
tasks = db.tasks.find_all(limit=20, offset=page*20, order_by="created_at DESC")
```

**Repeated Expensive Computation:**
```python
# BAD: Recomputes on every call
def get_user_stats(user_id):
    orders = db.orders.find_all(user_id=user_id)
    return compute_stats(orders)

# GOOD: Cache the result
from functools import lru_cache

@lru_cache(maxsize=128)
def get_user_stats(user_id):
    orders = db.orders.find_all(user_id=user_id)
    return compute_stats(orders)
```

**Redundant I/O:**
```python
# BAD: Reads config file on every call
def get_config():
    with open("config.json") as f:
        return json.load(f)

# GOOD: Read once, cache
_config = None
def get_config():
    global _config
    if _config is None:
        with open("config.json") as f:
            _config = json.load(f)
    return _config
```

### Step 4: Re-Measure
Run the same benchmark. Keep the change only if it moved the metric. Revert if it didn't.

### Step 5: Confirm Behavior Unchanged
Run the full test suite. Performance optimizations that break correctness are regressions.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "This should be faster" | Should isn't is. Measure before and after. |
| "Caching always helps" | Caching adds complexity, memory, and staleness bugs. Only cache what's measured to be slow. |
| "I'll optimize this while I'm here" | Scope creep. Focus on the measured bottleneck. |
| "This algorithm is O(n²) so it's slow" | For n=10, O(n²) is fine. Measure with real data sizes. |
| "Micro-optimizations add up" | They rarely do. Algorithmic wins (big-O, batching, caching) dominate. |

## Red Flags
- Optimizing without a measured baseline
- Adding complexity (caching, lazy loading, memoization) without proof it helps
- Optimizing code that isn't the bottleneck
- Breaking tests or behavior to gain performance
- Optimizing for small input sizes (premature optimization)
- Adding micro-optimizations (bit shifts, loop unrolling) instead of algorithmic wins

## Done when
- The target is met, proven by before/after numbers, with behaviour intact.

## Verification
- [ ] Baseline measured before optimization
- [ ] Bottleneck identified by profiling, not assumption
- [ ] After optimization, metric improved
- [ ] All existing tests pass
- [ ] `ruff check .` passes
- [ ] No new complexity added without measured benefit
