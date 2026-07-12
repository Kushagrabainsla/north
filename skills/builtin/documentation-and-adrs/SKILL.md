---
name: documentation-and-adrs
description: "Use when recording an architectural DECISION (an ADR) or updating user/developer docs that a change has made stale."
---
# Documentation and ADRs

> **Document the decision and its why - not just the what.**

## Use this when
- You made a non-obvious architectural choice worth recording, or a change made existing docs wrong.

## Do NOT use for
- Trivial changes that need no decision record, or restating what the code already makes clear.

## Procedure
1. For a decision, write a short ADR: context, the decision, alternatives considered, and consequences.
2. For docs, update exactly the sections the change affects - no more.
3. Keep it terse and current; place it where the next engineer will look.
4. Prefer explaining the code in the code; reserve docs for intent and rationale.

## Done when
- The decision's rationale is captured, or the affected docs match the new behaviour.
