---
name: exploring-an-unfamiliar-codebase
description: "Use when you must modify a repository or module you do not yet understand, before editing unfamiliar code."
---
# Exploring an Unfamiliar Codebase

> **Learn the intended behaviour from the tests before you change it.**

## Use this when
- You are about to edit a repo/module whose conventions and structure you do not already know.

## Do NOT use for
- Code you already understand, or a general research question (use the researcher).

## The Exploration Workflow

```
1. MAP → Entry points, module structure, public surface
2. READ → Tests define intended behavior and edge cases
3. TRACE → Follow one representative path end to end
4. NOTE → Conventions: error handling, naming, structure, style
5. PLAN → Only then plan the edit
```

## Procedure

### Step 1: Find Entry Points
Locate the module's public surface:
- **Python package:** `__init__.py`, `cli/main.py`, `app.py`, routes, CLI commands
- **Tests:** `tests/` directory — these document intended behavior
- **Config:** `pyproject.toml`, `setup.cfg`, `.env.example` — dependencies and settings
- **Docs:** `README.md`, `docs/`, `CONTRIBUTING.md` — conventions and architecture

### Step 2: Search for What You'll Touch
Use `search_files` to find the specific symbol/feature you will modify:
- Search by content: function names, class names, error messages
- Search by files: `*.py`, `*.toml`, `test_*.py`
- Read the file you'll modify AND its tests

### Step 3: Read the Tests First
Tests are the most honest documentation. They tell you:
- What the code is supposed to do (assertions)
- What edge cases matter (parameterized tests, fixtures)
- How the code is expected to be used (test setup/teardown)
- What error conditions are handled (pytest.raises, expected failures)

### Step 4: Trace One Path End to End
Follow a representative call from entry point to leaf. Understand:
- Data flow: what goes in, what comes out
- Error handling: what exceptions are raised and when
- Side effects: what gets modified (files, DB, network)
- Dependencies: what other modules are involved

### Step 5: Note Conventions
Before editing, catalog the project's patterns:
- **Error handling:** custom exceptions? `raise ... from`? error codes?
- **Naming:** `snake_case`? `camelCase`? prefix conventions?
- **Structure:** flat or nested? one module per file?
- **Style:** `ruff`? `black`? line length? import ordering?
- **Testing:** `pytest`? fixtures? mocking patterns? coverage?

### Step 6: Plan the Edit
Only after steps 1-5 should you plan your change. The plan should:
- Follow existing conventions exactly
- Place new code where similar code already lives
- Use the project's error handling patterns
- Be testable in the same way existing code is tested

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I can figure it out as I go" | You'll waste time fixing avoidable mistakes. 10 minutes of reading saves hours of debugging. |
| "The tests are too complicated to read" | Tests are the most honest docs. If they're hard to read, that's a signal about the code. |
| "I'll just copy what that other file does" | Copying without understanding copies bugs too. Understand first, then apply. |
| "This code is obviously wrong" | "Obviously" often means "I don't understand the context yet." Read more before judging. |
| "I don't need to read the tests" | Tests define the contract. Changing code without reading tests breaks the contract. |

## Red Flags
- Editing code before reading its tests
- Changing code you don't understand
- Ignoring existing patterns to use your preferred style
- Not checking what calls the function you're modifying
- Skipping the exploration because "it looks simple"
- Making changes that affect modules you haven't read

## Done when
- You can state what the code does, how it is tested, and which conventions your change must follow.

## Verification
- [ ] Read the tests for the code you'll modify
- [ ] Traced at least one path end to end
- [ ] Noted project conventions (error handling, naming, style)
- [ ] Plan follows existing patterns exactly
- [ ] `check_types` on changed files passes
- [ ] `ruff check .` passes
