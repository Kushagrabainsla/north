---
name: incremental-implementation
description: "Use when implementing any feature or change that touches more than one file, or when a task feels too big to land in one step. Delivers changes as thin vertical slices: implement, test, verify, commit, repeat."
---
# Incremental Implementation

> **Build in thin vertical slices. Each increment leaves the system in a working, testable state.**

## Use this when
- Implementing any multi-file change
- Building a new feature from a task breakdown
- Refactoring existing code across multiple files
- Any time you're tempted to write more than ~100 lines before testing

## Do NOT use for
- Single-file, single-function changes where scope is already minimal
- Pure config/docs changes with no logic

## The Increment Cycle

```
Implement -> Test -> Verify -> Commit -> Next slice
   ^                                      |
   +--------------------------------------+
```

For each slice:
1. **Implement** the smallest complete piece of functionality
2. **Test** - run the relevant tests (or write one if none exists)
3. **Verify** - `check_types` on changed files, `lint(path, fix=true)`
4. **Commit** - `git(action="add", args="path/to/file")` per file, then `git(action="commit", args="...")`
5. **Move to the next slice** - carry forward, don't restart

## Slicing Strategies

### Vertical Slices (Preferred)

Build one complete path through the stack:

```
Slice 1: Create a task (DB + API + basic UI)
 -> Tests pass, user can create a task via the UI

Slice 2: List tasks (query + API + UI)
 -> Tests pass, user can see their tasks

Slice 3: Edit a task (update + API + UI)
 -> Tests pass, user can modify tasks

Slice 4: Delete a task (delete + API + UI + confirmation)
 -> Tests pass, full CRUD complete
```

Each slice delivers working end-to-end functionality.

### Risk-First Slicing

Tackle the riskiest or most uncertain piece first:

```
Slice 1: Prove the WebSocket connection works (highest risk)
Slice 2: Build real-time task updates on the proven connection
Slice 3: Add offline support and reconnection
```

If Slice 1 fails, you discover it before investing in Slices 2 and 3.

### Contract-First Slicing

When backend and frontend need to develop in parallel:

```
Slice 0: Define the API contract (types, interfaces, OpenAPI spec)
Slice 1a: Implement backend against the contract + API tests
Slice 1b: Implement frontend against mock data matching the contract
Slice 2: Integrate and test end-to-end
```

## Implementation Rules

### Rule 0: Simplicity First

Before writing any code, ask: "What is the simplest thing that could work?"

After writing code, review it against these checks:
- Can this be done in fewer lines?
- Are these abstractions earning their complexity?
- Would a staff engineer look at this and say "why didn't you just...?"
- Am I building for hypothetical future requirements, or the current task?

Three similar lines of code is better than a premature abstraction.

### Rule 0.5: Scope Discipline

Touch only what the task requires.

Do NOT:
- "Clean up" code adjacent to your change
- Refactor imports in files you're not modifying
- Remove comments you don't fully understand
- Add features not in the spec because they "seem useful"
- Modernize syntax in files you're only reading

If you notice something worth improving outside your task scope, note it for the user rather than fixing it inline.

### Rule 1: One Thing at a Time

Each increment changes one logical thing. Don't mix concerns.

**Bad:** One commit that adds a new component, refactors an existing one, and updates the build config.

**Good:** Three separate commits - one for each change.

### Rule 2: Keep It Compilable

After each increment, the project must build and existing tests must pass. Don't leave the codebase in a broken state between slices.

### Rule 3: Feature Flags for Incomplete Features

If a feature isn't ready for users but you need to merge increments, use feature flags or keep changes behind conditions.

### Rule 4: Safe Defaults

New code should default to safe, conservative behavior. Disabled by default, opt-in.

### Rule 5: Rollback-Friendly

Each increment should be independently revertable:
- Additive changes (new files, new functions) are easy to revert
- Modifications to existing code should be minimal and focused
- Database migrations should have corresponding rollback migrations
- Avoid deleting something in one commit and replacing it in the same commit

## Increment Checklist

After each increment, verify with the project's own commands:

- [ ] The change does one thing and does it completely
- [ ] `check_types` on changed files passes (a "skipped" result is fine)
- [ ] `lint(path=..., fix=true)` passes
- [ ] Relevant tests pass via `bash` with adequate timeout
- [ ] The new functionality works as expected
- [ ] The change is committed with a descriptive message

## Working with north's agents

When implementing through the coder agent, the agent follows this discipline by default. The coder's plan (`update_plan`) naturally enforces incremental slices.

When you are the coder:
- Use `update_plan` with your intended steps (understand, design, change file A, verify, ...)
- Keep exactly one step `in_progress`
- Run `check_types` after every file change
- Run tests after every logical slice, not just at the end

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll test it all at the end" | Bugs compound. A bug in Slice 1 makes Slices 2-5 wrong. Test each slice. |
| "It's faster to do it all at once" | It *feels* faster until something breaks and you can't find which of 500 changed lines caused it. |
| "These changes are too small to commit separately" | Small commits are free. Large commits hide bugs and make rollbacks painful. |
| "This refactor is small enough to include" | Refactors mixed with features make both harder to review and debug. Separate them. |
| "Let me just quickly add this too" | Scope creep. Note it for later, stay focused on the current slice. |

## Red Flags
- More than 100 lines of code written without running tests
- Multiple unrelated changes in a single increment
- Skipping the test/verify step to move faster
- Build or tests broken between increments
- Large uncommitted changes accumulating
- Building abstractions before the third use case demands it
- Touching files outside the task scope "while I'm here"

## Verification
- [ ] Each increment was individually tested and committed
- [ ] The full test suite passes at the end
- [ ] The feature works end-to-end as specified
- [ ] No uncommitted changes remain
- [ ] Each commit has a descriptive message
