---
name: completing-a-cross-cutting-change
description: "Use when one semantic change must be propagated to many places - a renamed or changed symbol, function signature, config key, or contract used across the codebase."
---
# Completing a cross-cutting change

> **Grep for every reference; a half-applied change is a bug.**

## Use this when
- The same change (a rename, signature change, new required field, config key) touches multiple files or call sites.

## Do NOT use for
- A change contained to a single file or function.

## Procedure
1. Enumerate EVERY reference: search the symbol, string, and signature across the whole repo.
2. Update the definition and every caller.
3. Update the rest of the surface: tests, fixtures, config, wiring/registration, and inline or generated docs.
4. Re-search to confirm zero stale references remain.
5. Run the full relevant test suite - cross-cutting changes break distant callers.

## Done when
- No stale reference remains and the suite is green.
