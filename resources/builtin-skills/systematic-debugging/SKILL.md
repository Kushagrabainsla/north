---
name: systematic-debugging
description: "Use when a reproducible bug, error, or test failure has an unknown cause and you are about to fix it. Find the root cause before changing any code."
---
# Systematic Debugging

> **No fix without a reproduced failure and an identified root cause.**

## Use this when
- A test fails, an exception is thrown, or behaviour is wrong, and you don't yet know why.

## Do NOT use for
- Behaviour that changed between a known-good and known-bad revision (use `finding-a-regression`).
- Intermittent / CI-only / order-dependent failures (use `diagnosing-flaky-and-environment-failures`).

## The Stop-the-Line Rule

When anything unexpected happens:

1. **STOP** adding features or making changes
2. **PRESERVE** evidence (error output, logs, repro steps)
3. **DIAGNOSE** using the triage checklist
4. **FIX** the root cause
5. **GUARD** against recurrence
6. **RESUME** only after verification passes

**Don't push past a failing test or broken build.** Errors compound.

## Procedure

### Step 1: Reproduce
Make the failure happen reliably. If you can't reproduce it, you can't fix it with confidence.

```
Can you reproduce the failure?
├── YES → Proceed to Step 2
└── NO
    ├── Gather more context (logs, environment details)
    ├── Try reproducing in a minimal environment
    └── If truly non-reproducible, document conditions and monitor
```

### Step 2: Localize
Narrow down WHERE the failure happens:

```
Which layer is failing?
├── Python code → Check the traceback, read the function
├── Database → Check queries, schema, data integrity
├── External service → Check connectivity, API changes, rate limits
├── Configuration → Check .env, config files, environment variables
└── Test itself → Check if the test is correct (false negative)
```

**Use bisection for regression bugs:**
```bash
git bisect start
git bisect bad    # Current commit is broken
git bisect good <commit>  # This commit worked
# Git will checkout midpoint commits; run your test at each
git bisect run .venv/bin/python -m pytest tests/unit/test_foo.py::test_bar
```

### Step 3: Reduce
Create the minimal failing case:
- Remove unrelated code/config until only the bug remains
- Simplify the input to the smallest example that triggers the failure
- Strip the test to the bare minimum that reproduces the issue

### Step 4: Form ONE Hypothesis
Read the full error and stack trace to the deepest frame in code you can change. Form ONE hypothesis. Confirm it with a probe (a log line, assertion, or breakpoint) BEFORE editing.

### Step 5: Fix the Root Cause, Not the Symptom

Ask: "Why does this happen?" until you reach the actual cause, not just where it manifests.

```
Symptom: "The user list shows duplicate entries"

Symptom fix (bad):
  → Deduplicate in the UI: list(set(users))

Root cause fix (good):
  → The API endpoint has a JOIN that produces duplicates
  → Fix the query, add a DISTINCT, or fix the data model
```

### Step 6: Guard Against Recurrence
Write a test that catches this specific failure. It should fail without the fix and pass with it.

### Step 7: Verify End-to-End
```bash
.venv/bin/python -m pytest tests/unit/test_foo.py::test_bar -v
.venv/bin/python -m pytest tests/unit/ -q
.venv/bin/ruff check .
```

## Error-Specific Patterns

### Test Failure Triage
```
Test fails after code change:
├── Did you change code the test covers?
│   └── YES → Check if the test or the code is wrong
├── Did you change unrelated code?
│   └── YES → Likely a side effect → Check shared state, imports
└── Test was already flaky?
    └── Use diagnosing-flaky-and-environment-failures
```

### Import/Module Errors
```
ModuleNotFoundError:
├── Module exists? → Check installed: .venv/bin/pip show <pkg>
├── Import path correct? → Check __init__.py, module structure
└── Version mismatch? → Check pyproject.toml constraints
```

### Non-Reproducible Failures
```
Cannot reproduce on demand:
├── Timing-dependent? → Add timestamps, try with artificial delays
├── Environment-dependent? → Compare .env, Python version, OS
├── State-dependent? → Check for leaked state between tests
└── Truly random? → Add defensive logging, set up alerts
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The tests pass so the code is correct" | Tests prove the paths they cover. Bugs hide in paths they don't. |
| "I already know what the issue is" | Form ONE hypothesis and confirm with a probe before editing. Don't guess. |
| "This is too small to investigate" | Small bugs cause big outages. A one-line logic inversion can corrupt data. |
| "I'll just add a try/except here" | Swallowing errors hides the problem. Fix the root cause. |
| "It only fails in CI" | CI failures are real failures. Investigate the difference. |

## Red Flags
- Editing code before reproducing the failure
- Changing multiple things at once while debugging
- Swallowing errors with bare `except:` or broad exception catching
- Skipping the regression test because "it's obvious"
- Pushing past a failing test to work on the next feature
- Making the adversarial prompt soft ("does this look good?") instead of adversarial

## Done when
- The failure reproduces no more, a regression test locks it in, and you can name the root cause in one sentence.

## Verification
- [ ] Failure reproduces reliably before fix
- [ ] Root cause identified (not just symptom)
- [ ] Regression test written and passes
- [ ] Full test suite passes
- [ ] `ruff check .` passes
- [ ] Can name the root cause in one sentence
