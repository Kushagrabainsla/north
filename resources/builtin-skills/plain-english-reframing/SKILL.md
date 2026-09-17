---
name: plain-english-reframing
description: "Use when the user expresses confusion (\"wait what?\", \"explain simpler\") or requests interactive concept teaching, translating technical architecture into plain mental models."
domains:
  - general
  - engineering
---
# Plain-English Reframing

> **When communication breaks down, do not repeat the same technical jargon louder. Re-anchor into plain mental models, domain metaphors, and incremental verification.**

## Use this when
- The user expresses confusion (e.g., *"wait what?"*, *"I don't follow"*, *"explain like I'm 5"*, *"too complex"*).
- Explaining subtle architecture, state machines, or distributed systems concepts to non-technical or multi-disciplinary stakeholders.
- Conducting interactive teaching or walkthrough sessions on unfamiliar codebases.

## Do NOT use for
- Formal academic literature surveys (use `conducting-a-literature-review`).
- Standard API / code documentation where precise technical signatures are expected (use `documentation-and-adrs`).

---

## Core Philosophy: The Cognitive Re-Anchor

When an explanation fails to land:
1. **Identify the Missing Context Anchor**: The user is usually missing the foundational *mental model* or *why*, not the implementation mechanics.
2. **Strip Internal Acronyms & Jargon**: Replace implementation-heavy terms with everyday physical metaphors.
3. **Connect to Shared Domain Language**: Map concepts back to the project's core domain vocabulary.
4. **Calibrate with Quick Checkpoints**: Ask a 1-sentence calibration question to confirm alignment before proceeding.

---

## Reframing Patterns

| Technical Jargon (Fails to Land) | Plain-English Mental Model (Land Immediately) |
|---|---|
| *"We are triggering a materialization cascade on the AST nodes via a reactive subscription."* | *"When you edit a parent folder, all files inside it update their disk paths automatically, like updating a directory's street address."* |
| *"The idempotency key prevents duplicate side-effects during network partitions."* | *"If the internet cuts out and North sends the message twice, the system recognizes the receipt number and only charges once."* |
| *"We need an expand-contract migration to decouple database schema mutations."* | *"We build the new doorway next to the old one, move people over one by one, and only tear down the old door once everyone has switched."* |

---

## Procedure

### 1. Acknowledge and Reset Immediately
Do not defend the previous explanation. Immediately reset with a welcoming, clear stance:
> *"Let's take a step back and look at the big picture without the jargon."*

### 2. Identify the Core "Why"
State the purpose of the system or change in one sentence using everyday language:
- What is the real-world problem?
- What would happen if we didn't do this?

### 3. Build the Concrete Metaphor
Use a physical, mechanical, or visual analogy:
- Explain the moving parts (who sends what, who receives what, where it is stored).
- Highlight the single most important rule or constraint.

### 4. Bridge Back to the Code
Relate the simple mental model directly back to the project's files or functions:
- *"In the code, this happens in `router.py` when it checks whether a message is an email or a command."*

### 5. Check for Comprehension
Ask a single, non-condescending alignment check:
> *"Does that mental model make sense, or would you like to explore a specific part of how it works?"*

---

## Red Flags
- Repeating the exact same paragraph with slightly different synonyms.
- Using 5 new unexplained acronyms to explain 1 previous acronym.
- Blaming the user for not understanding implementation details.
- Skipping straight back into code editing before confirming the user is comfortable.

## Verification Checklist
- [ ] Jargon replaced with intuitive real-world metaphors
- [ ] Core "why" established before "how"
- [ ] Tied back to project domain concepts
- [ ] Confirmed understanding with a clear check-in question
