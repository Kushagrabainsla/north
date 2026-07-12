---
name: safe-refactoring
description: "Use when restructuring code whose behaviour must stay identical - extract, rename, move, or de-duplicate - and you are NOT changing what it does."
---
# Safe refactoring

> **Behaviour in == behaviour out; prove it with tests around the seams first.**

## Use this when
- You are improving structure (extract a function, rename, move, remove duplication) with no intended behaviour change.

## Do NOT use for
- Changing behaviour, adding a feature, or fixing a bug.

## Procedure
1. Identify the behaviour-preserving seam you will change.
2. Ensure characterization tests capture the CURRENT behaviour; add them if missing.
3. Refactor in small steps, running the tests green after each step.
4. Verify public signatures / return types are unchanged (or update every caller in the same change).
5. The final diff should change structure, not outcomes.

## Done when
- Structure is improved, every test that passed before still passes, and no observable behaviour changed.
