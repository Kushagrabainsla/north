# Modular Multi-Agent Orchestration — Implementation Plan

## Goal
Add four specialised agents (Researcher, Architect, Coder, Tester) to the north system. Each agent is fully self-contained and self-describing. Routing is fully dynamic — agents call each other via the existing `delegate_task` tool based on what the current situation requires. No hardcoded pipeline. Artifacts live in `{workspace}/.north/`.

---

## Architectural Principle: Dynamic Agent Delegation

Agents do not follow a fixed sequence. Each agent runs its own ReAct loop and decides mid-loop whether to delegate to another agent via the built-in `delegate_task` tool. Examples:

- Tester finds failing tests → delegates to Coder to fix → Coder delegates back to Tester to re-run
- Tester finds an architectural mismatch → delegates to Architect to update the spec
- Architect needs more context → delegates to Researcher

The orchestrator's job is only to **pick the right entry agent** for the task. Everything after that is agent-driven.

Each agent's `config.yaml` declares `accepts` (for entry point routing) and `produces` (documents what artifacts it writes — read by other agents when deciding who to delegate to).

---

## 1. Agent Modules

Four new folders under `agents/`, following the existing convention. Prompts live in `agent.py` or the system prompt — no `templates/` subfolder.

| Agent | `domain` | Responsibility |
|---|---|---|
| `researcher` | `engineering` | Gathers context, prior art, unknowns. Writes `.north/research/context.md` and `.north/research/references.json`. |
| `architect` | `engineering` | Produces spec and decision log. Reads Tester QA reports and updates docs after each test cycle. |
| `coder` | `engineering` | Implements against the spec. Reads `.north/architecture/spec.md`, writes `.north/implementation/implementation_notes.md`. |
| `tester` | `engineering` | Writes tests, runs them, manages QA report versioning, writes `.north/qa/qa_report_v{N}.md`. |

Each folder contains:
- `agent.py` — class extending `AgenticLLMAgent`.
- `config.yaml` — `name`, `domain`, `accepts`, `produces`, `model_pool`, `class_name`.
- `tools.yaml` — agent-specific tool bindings.

Each agent's system prompt must describe:
- Its own responsibility
- All four agents by name, what they do, and what they produce — so it can make informed `delegate_task` calls

---

## 2. Artifact Layout

All artifacts live under `{workspace}/.north/`. Whether to commit or `.gitignore` this directory is left to the user.

```
{workspace}/.north/
  research/
    context.md
    references.json
  architecture/
    spec.md
    decision_log.md
  implementation/
    implementation_notes.md
  qa/
    qa_report_v1.md
    qa_report_v2.md        # Tester increments version on each run
    coverage.txt
```

Paths are relative to `workspace`, which already flows through `AgentDependencies.workspace` → `_path.resolve_path` into all filesystem tools. No new plumbing needed.

---

## 3. Tester Agent — Versioning

The Tester agent owns its own versioning entirely:
- On each run, scans `.north/qa/` for existing `qa_report_v{N}.md` files and writes `v{max+1}`.
- No external tracker. No orchestrator involvement.
- Report includes: pass/fail summary, failing test names, error excerpts, coverage delta, suggested fixes.
- Both Architect and Coder read the latest report (highest N) when delegated to.

---

## 4. Orchestrator Routing

**No orchestrator changes needed.** The planner picks a single entry agent (`SINGLE_AGENT` mode) based on task scope — agents handle all further routing themselves via `delegate_task`.

Entry point heuristic (encoded in `prompts/planner.md`):
- New feature / greenfield → `researcher`
- Has a spec, needs building → `coder`
- Has implementation, needs validation → `tester`
- Small code fix → `coder` directly

`HIERARCHICAL` mode is **not used** — it pre-plans the sequence, which is the hardcoding we want to avoid.

---

## 5. Delegation Depth

`delegate_task` is already built into every `AgenticLLMAgent`. The current cap is `_MAX_DELEGATION_DEPTH = 2` ("top-level agent may delegate once; that delegate cannot delegate further"). This is too shallow for dynamic multi-agent chains.

**Change needed:** Raise `_MAX_DELEGATION_DEPTH` in `agents/agentic_llm_agent.py` from `2` to `6`. This allows chains like Tester → Coder → Tester → Architect without hitting the wall.

---

## 6. Code Agent Deprecation

`agents/code/agent.py` is marked deprecated (done). Deleted once the four new agents are stable.

---

## 7. Remaining Open Issues

1. **Raise `_MAX_DELEGATION_DEPTH`** — change from `2` to `6` in `agents/agentic_llm_agent.py`.

2. **`produces` field in `AgentConfig`** — add to `agents/models.py`, default empty list. Populated in each new agent's `config.yaml` so agents know what other agents produce when deciding who to delegate to.

3. **Planner prompt updates** — `prompts/planner.md` needs:
   - `engineering` added to the domain table, scoped to substantial tasks (implement a feature, build a system, refactor a module) — not small fixes
   - Entry point heuristic for which engineering agent to start with
   - `delegate_task` description updated to list the four new agents by name

---

## 8. Deliverables

- Four agent modules (`researcher`, `architect`, `coder`, `tester`) with config, tools, and system prompts that describe all four agents.
- `produces` field added to `AgentConfig` and each new agent's `config.yaml`.
- `_MAX_DELEGATION_DEPTH` raised to `6`.
- `prompts/planner.md` updated with `engineering` domain and entry point heuristic.
- `delegate_task` schema updated to list new agents.
