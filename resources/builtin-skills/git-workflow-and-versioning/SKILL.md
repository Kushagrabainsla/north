---
name: git-workflow-and-versioning
description: "Use when preparing a finished change set for review or release - commit hygiene and version-number decisions."
---
# Git workflow and versioning

> **One logical change per commit; the version reflects the change's impact.**

## Use this when
- You are finalizing work for a PR or a release and need clean history + a version decision.

## Do NOT use for
- Mid-implementation work, or the mechanics of opening a PR (north's deploy flow handles that).

## Procedure
1. Group the work into focused commits with imperative messages that say what and why.
2. Keep unrelated changes out of the commit/branch.
3. Choose the version bump by impact: breaking = major, new feature = minor, fix = patch.
4. Update the changelog entry to match.
5. Confirm the branch is clean and rebased before requesting review.

## Done when
- History is focused and readable, the version bump matches the impact, and the changelog is updated.
