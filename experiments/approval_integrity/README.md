# Approval-integrity benchmark

Date: 2026-09-28

## Decision

Three gates the 2026-09-26 review found open are closed at their cause, not
patched at the symptom:

- **Remembered approvals are learned under the key the policy recalls them
  by.** A card carries its action's identity (`Card.action_key`); the
  orchestrator records the answer under it. Since 64789a2 the two used
  different keys, so no approval was ever replayed.
- **"Inside the task's folder" is measured against what the server granted,
  never the model's `workspace` argument.** `AgentPayload.granted_workspace`
  and `ToolInput.granted_workspace` carry it; delegation can only narrow it.
  `write_file` goes through the same gate as `patch_file`. North's own
  scratch space (handoff and personal notes) is allowed in every mode, so
  unattended runs do not stall on a card.
- **The Telegram gateway fails closed.** It does not start without a valid,
  numeric allowlist, and an empty list admits nobody.

## Evidence

[`results.json`](results.json) records 21 cases run against production code -
the real tool registry, the agent loop's own dispatch, the approval store and
`Orchestrator.respond_approval`, and the Telegram gateway.

| | approval replay | file writes | Telegram | overall |
|---|---|---|---|---|
| before (2a1fe19) | 2/5 | 6/9 | 3/7 | 11/21 |
| after | 5/5 | 9/9 | 7/7 | 21/21 |

Every "before" failure is a finding from the review. The passing "before"
cases guard against over-blocking: edits inside the folder in AUTO, north's own
notes in interactive, and autonomous mode all still write.

Cost: a write auto-approved in AUTO went from 0.18 ms to 0.53 ms median (it now
leaves a resolved card behind and builds a diff).

## Reproduce

```bash
.venv/bin/python -m experiments.approval_integrity.benchmark --details
```

## Limitations

- Deterministic gate checks, not a model-quality or attack-success score.
  Prompt-injection resistance needs AgentDojo-style runs with a live model.
- The coding scoreboard (`evals/`), run on the installed north against the
  real `~/.north` in autonomous mode, scored 50% before (6/12 graded, 3
  timeouts) and 50% after (5/10 graded, 5 timeouts). Routing was pinned to one
  model, and both runs hit its rate limit, which caused every timeout and every
  fast failure; no failure came from an approval gate. So: no regression seen,
  but the run cannot detect a small one. A tasks-passing-but-`status=failed`
  pattern is the done-check refusing a review by the same pinned model.
- The bash "instantly safe" fast path is out of scope here; it is to be
  replaced by an OS sandbox rather than patched again.
