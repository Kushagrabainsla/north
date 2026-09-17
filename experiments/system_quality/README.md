# Cross-prompt system-quality benchmark

Date: 2026-09-16

## Decision

North uses one shared repository-overview intent rule for both quick-path
routing and evidence verification. Quick repository answers still require both
structure and source inspection, while deep, mutating, low-confidence, and
current-research prompts stay on the standard path. Reduced tool output must
retain diagnostic signals regardless of where they occur.

## Evidence

The recorded run in [`results.json`](results.json) covers 30 cases across four
repository shapes:

- 14 routing cases spanning direct, colloquial, deep, mutating, and
  low-confidence prompts;
- 12 evidence cases using valid and deliberately shallow tool traces; and
- 4 outputs from 8,000 to 64,000 characters with important evidence placed in
  the middle.

All 30 cases pass. The prompt variants caught a real pre-benchmark mismatch:
"What does this codebase do?" selected the quick profile but did not activate
the repository evidence gate. The shared production predicate fixes that drift.

## Reproduce

```bash
.venv/bin/python -m experiments.system_quality.benchmark --details
```

The harness imports the production routing, evidence, and output-reduction
functions. It does not duplicate their logic in an evaluator.

## Limitations

- This is a deterministic guardrail benchmark, not a model-quality score.
- Repository types are represented by prompt and tool-trace shapes; the harness
  does not ask a model to navigate cloned repositories.
- Coding correctness remains covered by the held-out tasks in `evals/`.
- Live model latency, answer quality, and tool-choice recall require separate
  repeated runs and must not be inferred from the 100% guardrail score.
