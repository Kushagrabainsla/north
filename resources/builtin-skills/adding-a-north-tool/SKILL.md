---
name: adding-a-north-tool
description: "Use when adding a new tool/capability to north, or when an agent needs an action no existing tool provides."
intents: [create-tool]
---
# Adding a tool to north

> **Follow north's tool conventions exactly, or the tool will not join the global catalog.**

## Use this when
- north needs a new capability, or you must extend/create a `Tool` under `tools/`.

## Do NOT use for
- Something an existing tool already does, or a one-off that `write_file`/`bash` handles directly.

## Procedure
1. Check `tools/universal/`, `tools/analysis/`, `tools/semantic/`, and `tools/specialized/` for a tool to use or extend. Only add a new one if none fits.
2. Create a `Tool` subclass: set `name` (snake_case, unique) + `description` (what agents match on) + `parameters_schema` (OpenAI JSON Schema); set `is_mutating = True` only if it writes state; implement `async def run(...) -> ToolOutput`, returning `ToolOutput(success=False, error=...)` on recoverable errors rather than raising.
3. Placement under `tools/universal/`, `tools/specialized/`, `tools/analysis/`, or `tools/semantic/` is organizational only. Every discovered tool is eligible for every agent and is selected by task relevance.
4. Needs constructor args? Auto-discovery skips it - register it manually in `orchestrator/app.py` (mirror `ScheduleTaskTool`). Registration adds it to the same global catalog.
5. Add a unit test under `tests/unit/tools/` covering the success path and one failure path.

## Done when
- The tool is discovered and works, has a passing test, and `.venv/bin/python -m pytest -q` + `.venv/bin/ruff check .` are green.
