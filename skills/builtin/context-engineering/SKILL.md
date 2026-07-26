---
name: context-engineering
description: "Use when starting a new session, when agent output quality degrades, when switching between tasks, or when configuring rules and context for a project. Feed agents the right information at the right time."
---
# Context Engineering

> **Context is the single biggest lever for agent output quality — too little and the agent hallucinates, too much and it loses focus.**

## Use this when
- Starting a new coding session or task
- Agent output quality is declining (wrong patterns, hallucinated APIs, ignoring conventions)
- Switching between different parts of a codebase
- Setting up a new project for AI-assisted development
- The agent is not following project conventions

## Do NOT use for
- Single-file changes with clear scope
- When the agent already has sufficient context
- Mechanical operations (renaming, formatting)

## The Context Hierarchy

Structure context from most persistent to most transient:

```
1. Rules Files (pyproject.toml, .env, AGENTS.md)    ← Always loaded, project-wide
2. Spec / Architecture Docs                           ← Loaded per feature/session
3. Relevant Source Files                               ← Loaded per task
4. Error Output and Logs                              ← Loaded when debugging
5. Conversation History                               ← Session-scoped
```

## How to Pack Context

### For Rules Files (Level 1)
- Keep them under 500 lines — agents ignore oversized context
- Put project-wide conventions first, specifics later
- Use concrete examples, not abstract descriptions
- Update when conventions change — stale rules are worse than no rules

### For Specs (Level 2)
- Include: what to build, acceptance criteria, constraints
- Include: relevant API contracts, data models, type definitions
- Exclude: implementation details the agent should figure out
- Keep under 300 lines per feature

### For Source Files (Level 3)
- Load only what's relevant to the current task
- Include tests — they define the contract
- Include the file you'll modify AND its callers/callees
- Don't load the entire codebase — the agent will lose focus

### For Error Output (Level 4)
- Include the full traceback (not just the last line)
- Include the relevant code context (5-10 lines around the error)
- Include environment details (Python version, OS, key dependencies)
- Don't include irrelevant log noise

## Confusion Management

When the agent seems confused or output quality degrades:

1. **Check for context conflicts.** Rules file says one thing, spec says another. Resolve the conflict.
2. **Check for context starvation.** The agent doesn't have enough info. Add the missing context.
3. **Check for context flooding.** The agent has too much info. Trim to the essentials.
4. **Check for stale context.** The rules or spec are outdated. Update them.

## North-Specific Context Patterns

North's architecture IS a context system. Here's how context flows:

```
User Task → Orchestrator → Agent Selection → Skill Loading → Context Building
                                                                    ↓
                                                            Agent Prompt + Rules + Skills + Source
                                                                    ↓
                                                            Agent Execution (coder/researcher/etc.)
                                                                    ↓
                                                            Output → Ledger → Next Agent
```

- **Agent prompts** (`agents/*/prompts/system.md`) define the agent's role and constraints
- **Skills** (`skills/builtin/*/SKILL.md`) are loaded on-demand based on task relevance
- **Rules** (`pyproject.toml`, `.env`) define project-wide configuration
- **The ledger** tracks state across agents and sessions

When building new skills or agents, ensure context is:
- **Focused:** Each agent sees only what it needs
- **Fresh:** Avoid context from stale sessions or outdated specs
- **Complete:** Include enough that the agent doesn't have to guess

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "More context is always better" | Context flooding makes agents lose focus. Curate ruthlessly. |
| "The agent should figure it out" | If you have the info, provide it. Guessing wastes tokens and time. |
| "I'll update the rules later" | Stale rules actively mislead. Update when conventions change. |
| "The spec is too long to read" | If it's too long for you, it's too long for the agent. Shorten it. |
| "Just throw everything in" | Dumping everything is the opposite of engineering. Be deliberate. |

## Red Flags
- Rules file over 500 lines (agents ignore oversized context)
- Stale rules that contradict current code
- Loading entire codebase for a single-file change
- Missing error context when debugging (just the exception, no traceback)
- Agent repeating mistakes from earlier in the session (context not being leveraged)
- Multiple conflicting sources of truth (rules say X, code does Y)

## Done when
- Context is focused, fresh, and complete for the task at hand.

## Verification
- [ ] Rules file is under 500 lines and current
- [ ] Only relevant source files are loaded
- [ ] Error output includes full tracebacks
- [ ] No conflicting context (rules vs code vs spec)
- [ ] Agent can complete the task without guessing
