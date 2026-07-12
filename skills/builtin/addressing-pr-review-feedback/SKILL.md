---
name: addressing-pr-review-feedback
description: "Use AFTER a pull request exists and has review comments, failing CI checks, or merge conflicts to resolve."
---
# Addressing PR review feedback

> **Resolve every comment explicitly - fix it, or reply why not.**

## Use this when
- A PR is open and has review comments, red CI, or conflicts.

## Do NOT use for
- Pre-PR implementation work.

## Procedure
1. Read every review comment and the CI logs.
2. Reproduce each failing check locally before "fixing" it.
3. Resolve conflicts by understanding BOTH sides - never blind-accept one.
4. Address each comment with a change or a reasoned reply.
5. Re-run the checks, push, and summarize what changed per comment.

## Done when
- CI is green, conflicts are resolved, and every comment has a fix or a reply.
