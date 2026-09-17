---
name: finding-a-regression
description: "Use when something that used to work now fails and you need to find which change caused it (a known-good and a known-bad revision both exist)."
---
# Finding a regression

> **Find the commit that introduced it before you fix it.**

## Use this when
- A feature that worked before is now broken, and you can point to a revision where it was fine.

## Do NOT use for
- A brand-new feature or a bug with no prior working state (use `systematic-debugging`).

## Procedure
1. Confirm a good revision and a bad revision.
2. Write a one-command test that passes on good and fails on bad.
3. `git bisect start; git bisect bad; git bisect good <rev>` and run that test at each step (or binary-search history by hand).
4. Inspect the culprit commit's diff to understand what changed and why it broke.
5. Fix at the root cause, not by reverting blindly, and add a regression test.

## Done when
- The introducing commit is identified, the fix is targeted, and a test prevents the regression returning.
