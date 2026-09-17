---
name: doubt-driven-development
description: "Use when a non-trivial decision must be verified before it stands - architectural choices, security-sensitive logic, threading/concurrency claims, or any assertion that would be cheaper to check now than to debug later. Biased to DISPROVE, not approve."
---
# Doubt-Driven Development

> **A confident answer is not a correct one. Verify non-trivial decisions with adversarial review before they become bugs.**

## Use this when
- About to commit non-trivial code (branching logic, module boundaries, state mutations)
- Asserting a property the type system cannot verify (thread safety, idempotence, ordering)
- Making an architectural decision under uncertainty
- Working in code you don't fully understand
- Stakes are high: production logic, security, irreversible data changes
- About to claim a non-obvious fact ("this is safe", "this scales", "this matches the spec")

## Do NOT use for
- Mechanical operations: renaming, formatting, file moves
- One-line changes with obvious correctness
- Following a clear, unambiguous user instruction
- Reading or summarizing existing code
- Pure tooling operations (running tests, listing files)
- When the user has explicitly asked for speed over verification

## The Process

### Step 1: CLAIM - Surface what stands

Name the decision in two or three lines:

```
CLAIM: "The new caching layer is thread-safe under the
 read-heavy workload described in the spec."
WHY THIS MATTERS: a race here corrupts user data and is
 hard to detect in QA.
```

If you can't write the claim that compactly, you have a vibe, not a decision. Surface it before scrutinizing it.

### Step 2: EXTRACT - Isolate the reviewable unit

A reviewer needs the **artifact** and the **contract**, not your reasoning journey.

- **Code**: the diff or the function - not the whole file
- **Decision**: the proposal in 3-5 sentences plus the constraints it must satisfy
- **Assertion**: the claim plus the evidence that supposedly supports it

Strip your reasoning. If you hand over conclusions, you'll get back validation of your conclusions. The unit must be small enough that a reviewer can hold it in mind in one read. If it's a 500-line PR, decompose first.

### Step 3: DOUBT - Invoke adversarial review

The reviewer's prompt **must be adversarial**. Framing decides the answer.

```
Adversarial review. Find what is wrong with this artifact.
Assume the author is overconfident. Look for:
- Unstated assumptions
- Edge cases not handled
- Hidden coupling or shared state
- Ways the contract could be violated
- Existing conventions this might break
- Failure modes under unexpected input

Do NOT validate. Do NOT summarize. Find issues, or state
explicitly that you cannot find any after thorough examination.

ARTIFACT: [paste the code/decision/claim]
CONTRACT: [what it must satisfy]
```

Pass ARTIFACT + CONTRACT only. **Do NOT pass the CLAIM.** Handing the reviewer your conclusion biases it toward agreement.

**How to invoke in north:**

For the coder agent: use `delegate_task(agent="researcher", ...)` to get a fresh-context investigation of whether your approach is sound. The researcher carries no prior reasoning bias.

For the architect agent: use `delegate_task(agent="architect", ...)` to challenge a design decision with fresh context.

In the orchestrator loop: the `critic` setting (when enabled) triggers a pre-implementation doubt cycle automatically.

### Step 4: RECONCILE - Classify every finding

For each finding from the reviewer:

| Finding type | Action |
|---|---|
| **Bug** (correctness/security) | Fix before proceeding |
| **Assumption** (unstated but reasonable) | Document in spec/plan, proceed |
| **Nit** (style/preference) | Note, don't block |
| **False positive** (reviewer missed context) | Discard with explanation |

### Step 5: STOP - When to stop iterating

Stop when:
- All findings are classified as nits or false positives
- You've done 3 doubt cycles on the same claim (diminishing returns)
- The user has explicitly said to proceed

## Procedure

1. **CLAIM** - Name the decision in 2-3 lines: what you believe and why it matters. If you can't write it that compactly, you have a vibe, not a decision.
2. **EXTRACT** - Isolate the smallest reviewable unit: the diff/function (not the whole file), the proposal in 3-5 sentences, plus the constraints it must satisfy. Strip your reasoning.
3. **DOUBT** - Invoke adversarial review with fresh context. Pass ARTIFACT + CONTRACT only (never the CLAIM). Use `delegate_task(agent="researcher")` for a fresh-context investigation.
4. **RECONCILE** - Classify every finding: Bug (fix before proceeding), Assumption (document and proceed), Nit (note, don't block), False Positive (discard with explanation).
5. **STOP** - All findings classified, 3 cycles max on same claim, or user says proceed.

## When to apply the cycle

| Decision type | Doubt level |
|---|---|
| Changing public API signatures | Full cycle (Steps 1-5) |
| Threading / concurrency / shared state | Full cycle |
| Security boundary (auth, input parsing) | Full cycle |
| New module or service boundary | Full cycle |
| Internal refactor, tests pass | Quick self-review (Step 3 only) |
| Config change, one file | Skip |

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Tests pass, so it's correct" | Tests prove the paths they cover. Doubt covers the paths they don't. |
| "I already thought about edge cases" | Long sessions turn assumptions into "facts". A fresh eye catches what yours glazed over. |
| "This is too small to review" | Small changes cause big outages. A one-line logic inversion can corrupt data. |
| "The reviewer will catch it later" | Later is more expensive. Doubt is cheap when the fix is a revert, expensive after 5 more commits. |
| "I'm sure about this" | Confidence is not evidence. Name the evidence or verify it. |

## Red Flags
- Skipping doubt because "tests pass" without checking what the tests actually cover
- Making the adversarial prompt soft ("does this look good?") instead of adversarial ("find what's wrong")
- Running doubt on the full file instead of the focused diff/artifact
- Ignoring findings because they're inconvenient
- Applying doubt to every trivial change (analysis paralysis)

## Verification
- [ ] Every non-trivial decision has a named CLAIM
- [ ] The review was genuinely adversarial (seeking disproof, not validation)
- [ ] Every finding was classified (bug/assumption/nit/false positive)
- [ ] Bugs were fixed before proceeding
- [ ] Assumptions were documented in spec or plan
