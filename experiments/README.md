# North experiments

This directory stores reproducible evidence behind architecture decisions.
Each experiment owns its inputs, runnable harness, recorded results, limitations,
and the production decision those results informed. Product code may reuse the
same primitives, but it must not import recorded results.

## Experiments

- [`approval_integrity/`](approval_integrity/) — approval replay, server-granted
  file-write scope, and the Telegram allowlist, before and after the fix.
- [`tool_selection/`](tool_selection/) — capability-catalog size and runtime
  tool-retrieval strategy.
- [`system_quality/`](system_quality/) — quick-path routing, task-specific
  evidence gates, and tool-output signal retention across prompt/repository
  shapes.
- [`coding_agents_integration/`](coding_agents_integration/) — whether a coding agent's
  approval requests, waits, runs and restarts connect to north's real cards, queue,
  dashboard and recovery, before the feature is built.
