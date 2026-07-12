---
name: dependency-management
description: "Use when adding, upgrading, or removing a third-party library or package."
---
# Dependency management

> **Check what you already have before adding a new dependency.**

## Use this when
- You are about to add, bump, or remove a third-party package.

## Do NOT use for
- Code that only uses libraries already present.

## Procedure
1. Check the existing dependencies and the standard library first - do not add what you already have.
2. Prefer a maintained, appropriately-licensed library with a healthy release history.
3. Add it to the manifest AND the lockfile; pin a sensible version constraint.
4. Import it and run the build/install to confirm it resolves.
5. Run the test suite.
6. To remove one, grep for every usage first and delete them all.

## Done when
- The dependency is declared + locked + resolving, or fully removed with no dangling import, and tests pass.
