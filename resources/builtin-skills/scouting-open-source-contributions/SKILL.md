---
name: scouting-open-source-contributions
description: "Use when asked to find an open-source project or issue to contribute to and prepare a pull request for it."
---
# Scouting open-source contributions

> **Only pick an issue you can fully finish and verify.**

## Use this when
- The task is to find external OSS work to do and open a PR - not work in your own repo.

## Do NOT use for
- Building or shipping in your own project (use the normal build + deploy flow).

## Procedure
1. Search GitHub for active, maintained repos in the target area (recent commits, accepts contributions).
2. Filter issues: `good-first-issue`, `help-wanted`, reproducible bugs, failing tests, or a clear TODO.
3. Assess feasibility - can you reproduce, fix, AND test it? Drop it if not.
4. Read the project's CONTRIBUTING guide and match its code style and tests.
5. Fork/branch, implement the fix WITH tests, and follow their PR template; reference the issue.
6. Open the PR, then use `addressing-pr-review-feedback` for follow-ups.

## Done when
- A feasible issue is fixed with tests on a branch, and a PR that follows the project's conventions is open.
