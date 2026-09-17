---
name: alignment-and-grilling
description: "Use when planning a complex feature, resolving ambiguous architecture decisions, or stress-testing a proposed plan before execution through a structured decision-tree interview."
domains:
  - engineering
  - general
---
# Alignment and Grilling

> **Misalignment is the costliest failure mode in software development. Resolve every branch of the design tree before writing code.**

## Use this when
- Planning a complex feature, refactor, or multi-step workflow.
- Requirements have multiple interpretations, trade-offs, or unspecified constraints.
- You or the user want to stress-test a design before execution.

## Do NOT use for
- Simple one-off questions or bug fixes where root cause and solution are unambiguous (use `systematic-debugging`).
- Pure exploratory codebase investigations without proposed changes (use `exploring-an-unfamiliar-codebase`).

---

## Core Philosophy: The Decision Tree & Frontier

Every feature or architecture decision is a **design tree**: root choices branch into dependent decisions.

* **The Frontier**: The set of questions whose prerequisites are already settled. These are the questions you can ask *now* without guessing answers to questions you haven't asked yet.
* **Batching by Round**: Never ask questions whose premises depend on unanswered choices. Ask the entire frontier in one structured round.
* **Propose, Don't Just Ask**: For every question on the frontier, always provide your recommended answer (`➡️ Recommended`) with rationale. This eliminates user decision fatigue.

---

## Procedure

### 1. Identify Root Goals and Scope
Before asking questions, read existing project context, ADRs, schemas, and recent commits. Formulate the core problem statement:
- What user problem does this solve?
- What are the non-negotiables (performance, backward compatibility, latency, privacy)?

### 2. Map the Decision Frontier
Identify the immediate branch points:
- What must be chosen first (e.g., storage schema, sync vs async, API contract)?
- Filter out questions that depend on choices not yet made.

### 3. Conduct Structured Grilling Rounds
Format each question on the frontier clearly:

```markdown
❓ **Q1 — [Short Title]**: [Context and tradeoff description]
- A) [Option A]
- B) [Option B]
- C) [Option C]

➡️ **Recommended**: [Option A] — [1-2 sentences on why this fits best given project constraints]
---
❓ **Q2 — [Short Title]**: ...
```

Wait for user response before generating the next frontier round.

### 4. Update Project Glossary and Context
When domain concepts or specialized terminology emerge during the grilling session:
- Standardize the name (avoid generic terms like `manager`, `handler`, `data_processor`).
- Record newly established invariants, constraints, and vocabulary in the task plan or architectural notes.

### 5. Synthesize into an Unambiguous Specification
Once the decision tree frontier is fully resolved:
- Produce a clear, actionable implementation plan with agreed-upon seams, interfaces, and verification criteria.
- Confirm readiness to proceed to execution.

---

## Red Flags
- Asking 10 speculative questions when answering Q1 renders Q2-Q10 irrelevant.
- Asking open-ended questions without providing a recommended default.
- Starting implementation with unverified assumptions on critical seams.
- Inconsistent naming across grilling rounds.

## Verification Checklist
- [ ] Prerequisites settled before asking dependent questions
- [ ] Every question includes a recommended default with rationale
- [ ] Domain terms standardized and recorded
- [ ] Final plan signed off with clear acceptance criteria
