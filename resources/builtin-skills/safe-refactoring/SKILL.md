---
name: safe-refactoring
description: "Use when restructuring code whose behaviour must stay identical - extract, rename, move, or de-duplicate - and you are NOT changing what it does."
---
# Safe Refactoring

> **Behaviour in == behaviour out; prove it with tests around the seams first.**

## Use this when
- You are improving structure (extract a function, rename, move, remove duplication) with no intended behaviour change.

## Do NOT use for
- Changing behaviour, adding a feature, or fixing a bug.

## The Five Principles

1. **Preserve behavior exactly.** Same inputs, outputs, side effects, error behavior, and edge cases. If in doubt, check with the user.
2. **Follow project conventions.** Study neighboring code first. Match naming, error handling, and type annotation style.
3. **Clarity over cleverness.** Explicit code beats compact code. A helper function with a clear name beats an inlined expression.
4. **Maintain balance.** Don't inline a helper that gave a concept a name. Don't combine two simple functions into one complex one.
5. **Scope to what changed.** Avoid drive-by refactors of unrelated code. Unscoped refactoring creates noisy diffs and risks regressions.

## Procedure

1. **Understand before touching (Chesterton's Fence).** Before changing or removing anything, understand why it exists. Answer: What is this code's responsibility? What calls it? What does it call? What are the edge cases?
2. **Ensure characterization tests capture CURRENT behaviour.** Add them if missing. These are your safety net.
3. **Refactor in small steps.** Run `check_types` on changed files after each step. Run relevant tests via `bash` with adequate timeout. Every step must leave the system in a passing state.
4. **Verify public signatures / return types are unchanged** (or update every caller in the same change).
5. **Run `lint(path=..., fix=true)` on all changed files** to match project style.
6. **Commit after each logical slice.** Don't batch unrelated refactors into one commit.

## Common Patterns

| Pattern | When | Example |
|---------|------|---------|
| Extract function | Long function doing multiple things | Pull a 20-line block into `def _validate_input(data)` |
| Extract variable | Complex expression used multiple times | `is_valid = len(errors) == 0 and all(...)` |
| Inline function | Thin wrapper adding no value | Remove `get_user(id)` that just calls `db.get(id)` |
| Rename | Name doesn't match behavior | `process` -> `validate_and_store` if that's what it does |
| Remove dead code | Unreachable branches, unused imports | Delete after confirming truly dead (grep for references) |
| Deduplicate | Same 5+ lines in multiple places | Extract to a shared function with a descriptive name |

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "It's working, no need to touch it" | Working code that's hard to read will be hard to fix when it breaks. |
| "I'll just quickly clean up this unrelated code too" | Unscoped refactoring creates noisy diffs and risks regressions. |
| "This abstraction might be useful later" | Don't preserve speculative abstractions. Remove and re-add when needed. |
| "The original author must have had a reason" | Check git blame (Chesterton's Fence). But accumulated complexity often has no reason. |
| "Fewer lines is always simpler" | A 1-line nested ternary is not simpler than a 5-line if/else. |
| "I'll refactor while adding this feature" | Separate refactoring from feature work. Mixed changes are harder to review and revert. |

## Red Flags
- Simplification that requires modifying tests to pass (you likely changed behavior)
- "Simplified" code that is longer and harder to follow than the original
- Renaming things to match your preferences rather than project conventions
- Removing error handling because "it makes the code cleaner"
- Refactoring code you don't fully understand
- Batching many refactors into one large commit
- Touching files outside the scope of the current task

## Done when
- Structure is improved, every test that passed before still passes, and no observable behaviour changed.

## Verification
- [ ] All existing tests pass without modification
- [ ] `check_types` on changed files passes
- [ ] `ruff check .` passes
- [ ] Each refactor is a reviewable, incremental change
- [ ] No error handling was removed or weakened
- [ ] No dead code was left behind
