---
name: code-simplification
description: "Use when refactoring code for clarity without changing behavior - when code works but is harder to read, maintain, or extend than it should be, or when reviewing code that has accumulated unnecessary complexity."
---
# Code Simplification

> **Not fewer lines - code that is easier to read, understand, modify, and debug.**

## Use this when
- After a feature is working and tests pass, but the implementation feels heavier than it needs to be
- During code review when readability or complexity issues are flagged
- When you encounter deeply nested logic, long functions, or unclear names
- When refactoring code written under time pressure
- When consolidating related logic scattered across files

## Do NOT use for
- Code is already clean and readable - don't simplify for the sake of it
- You don't understand what the code does yet - comprehend before you simplify
- The code is performance-critical and the "simpler" version would be measurably slower
- You're about to rewrite the module entirely - simplifying throwaway code wastes effort

## The Five Principles

### 1. Preserve Behavior Exactly

Don't change what the code does - only how it expresses it. All inputs, outputs, side effects, error behavior, and edge cases must remain identical.

```
ASK BEFORE EVERY CHANGE:
-> Does this produce the same output for every input?
-> Does this maintain the same error behavior?
-> Does this preserve the same side effects and ordering?
-> Do all existing tests still pass without modification?
```

### 2. Follow Project Conventions

Simplification means making code more consistent with the codebase, not imposing external preferences. Before simplifying:
1. Study how neighboring code handles similar patterns
2. Match the project's style for naming, error handling, and type annotations
3. Follow north's conventions: `ruff` formatting, 120-char line length, `snake_case`

Simplification that breaks project consistency is churn, not simplification.

### 3. Prefer Clarity Over Cleverness

Explicit code is better than compact code when the compact version requires a mental pause to parse.

```python
# UNCLEAR: Nested ternary chain
label = "New" if is_new else ("Updated" if is_updated else ("Archived" if is_archived else "Active"))

# CLEAR: Early returns
def get_status_label(item):
    if item.is_new:
        return "New"
    if item.is_updated:
        return "Updated"
    if item.is_archived:
        return "Archived"
    return "Active"
```

```python
# UNCLEAR: Dense dict comprehension with side effects
result = {k: v for k, v in ((i.id, transform(i)) for i in items if i.active)}

# CLEAR: Named steps
active_items = [i for i in items if i.active]
result = {i.id: transform(i) for i in active_items}
```

### 4. Maintain Balance

Simplification has a failure mode: over-simplification. Watch for these traps:
- **Inlining too aggressively** - removing a helper that gave a concept a name makes the call site harder to read
- **Combining unrelated logic** - two simple functions merged into one complex function is not simpler
- **Removing "unnecessary" abstraction** - some abstractions exist for extensibility or testability
- **Optimizing for line count** - fewer lines is not the goal; easier comprehension is

### 5. Scope to What Changed

Default to simplifying recently modified code. Avoid drive-by refactors of unrelated code unless explicitly asked. Unscoped simplification creates noisy diffs and risks unintended regressions.

## The Simplification Process

### Step 1: Understand Before Touching (Chesterton's Fence)

Before changing or removing anything, understand why it exists. If you see a fence across a road and don't understand why it's there, don't tear it down.

```
BEFORE SIMPLIFYING, ANSWER:
- What is this code's responsibility?
- What calls it? What does it call?
- What are the edge cases and error paths?
- Are there tests that define the expected behavior?
- Why might it have been written this way? (Performance? Platform constraint?)
```

If you can't answer these, you're not ready to simplify. Read more context first.

### Step 2: Identify Simplification Opportunities

Scan for these patterns:

**Structural complexity:**

| Pattern | Signal | Simplification |
|---------|--------|----------------|
| Deep nesting (3+ levels) | Hard to follow control flow | Extract into guard clauses or helper functions |
| Long functions (50+ lines) | Multiple responsibilities | Split into focused functions with descriptive names |
| Nested ternaries | Requires mental stack to parse | Replace with if/elif/else chains |
| Boolean parameter flags | `do_thing(True, False, True)` | Replace with options dict or separate functions |
| Repeated conditionals | Same `if` check in multiple places | Extract to a well-named predicate function |

**Naming and readability:**

| Pattern | Signal | Simplification |
|---------|--------|----------------|
| Generic names | `data`, `result`, `temp`, `val` | Rename to describe content: `user_profile`, `validation_errors` |
| Abbreviated names | `usr`, `cfg`, `btn`, `evt` | Use full words unless abbreviation is universal (`id`, `url`, `api`) |
| Misleading names | Function named `get` that also mutates | Rename to reflect actual behavior |
| Comments explaining "what" | `# increment counter` above `count += 1` | Delete - the code is clear enough |
| Comments explaining "why" | `# Retry because the API is flaky under load` | Keep - they carry intent the code can't express |

**Redundancy:**

| Pattern | Signal | Simplification |
|---------|--------|----------------|
| Duplicated logic | Same 5+ lines in multiple places | Extract to a shared function |
| Dead code | Unreachable branches, unused variables | Remove (after confirming truly dead) |
| Unnecessary abstractions | Wrapper that adds no value | Inline the wrapper |
| Over-engineered patterns | Factory-for-a-factory, strategy-with-one-strategy | Replace with the simple direct approach |

### Step 3: Apply Changes Incrementally

Make one simplification at a time. Run tests after each change. Submit refactoring changes separately from feature or bug fix changes.

```
FOR EACH SIMPLIFICATION:
1. Make the change
2. Run check_types on the file
3. Run lint(path=..., fix=true)
4. Run relevant tests
5. If tests pass -> commit (or continue to next simplification)
6. If tests fail -> revert and reconsider
```

Avoid batching multiple simplifications into one untested change. If something breaks, you need to know which simplification caused it.

### Step 4: Verify the Result

After all simplifications, step back and evaluate:

```
COMPARE BEFORE AND AFTER:
- Is the simplified version genuinely easier to understand?
- Did you introduce patterns inconsistent with the codebase?
- Is the diff clean and reviewable?
- Would a teammate approve this change?
```

If the "simplified" version is harder to understand or review, revert.

## Language-Specific Guidance (Python)

```python
# SIMPLIFY: Verbose dictionary building
# Before
result = {}
for item in items:
    result[item.id] = item.name
# After
result = {item.id: item.name for item in items}

# SIMPLIFY: Nested conditionals with early return
# Before
def process(data):
    if data is not None:
        if data.is_valid():
            if data.has_permission():
                return do_work(data)
            else:
                raise PermissionError("No permission")
        else:
            raise ValueError("Invalid data")
    else:
        raise TypeError("Data is None")
# After
def process(data):
    if data is None:
        raise TypeError("Data is None")
    if not data.is_valid():
        raise ValueError("Invalid data")
    if not data.has_permission():
        raise PermissionError("No permission")
    return do_work(data)

# SIMPLIFY: Unnecessary else after early return
# Before
def calculate(x):
    if x > 0:
        return x * 2
    else:
        return 0
# After
def calculate(x):
    if x > 0:
        return x * 2
    return 0
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "It's working, no need to touch it" | Working code that's hard to read will be hard to fix when it breaks. |
| "Fewer lines is always simpler" | A 1-line nested ternary is not simpler than a 5-line if/else. |
| "I'll just quickly simplify this unrelated code too" | Unscoped simplification creates noisy diffs and risks regressions. |
| "This abstraction might be useful later" | Don't preserve speculative abstractions. Remove and re-add when needed. |
| "The original author must have had a reason" | Check git blame (Chesterton's Fence). But accumulated complexity often has no reason. |
| "I'll refactor while adding this feature" | Separate refactoring from feature work. Mixed changes are harder to review and revert. |

## Red Flags
- Simplification that requires modifying tests to pass (you likely changed behavior)
- "Simplified" code that is longer and harder to follow than the original
- Renaming things to match your preferences rather than project conventions
- Removing error handling because "it makes the code cleaner"
- Simplifying code you don't fully understand
- Batching many simplifications into one large commit
- Refactoring code outside the scope of the current task without being asked

## Verification
- [ ] All existing tests pass without modification
- [ ] `ruff check .` passes (north uses ruff, line length 120)
- [ ] Each simplification is a reviewable, incremental change
- [ ] The diff is clean - no unrelated changes mixed in
- [ ] Simplified code follows project conventions
- [ ] No error handling was removed or weakened
- [ ] No dead code was left behind
