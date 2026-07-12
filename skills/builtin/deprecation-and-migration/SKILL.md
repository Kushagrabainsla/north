---
name: deprecation-and-migration
description: "Use when removing or renaming a public symbol, API, or config key that other code depends on - staging the change so callers are not broken in one step."
---
# Deprecation and migration

> **Do not break callers in one step; deprecate, then remove.**

## Use this when
- You must retire or rename something callers depend on (a public function, endpoint, flag, or config key).

## Do NOT use for
- Persisted data / DB / on-disk format changes (use `data-and-schema-migration`).
- Purely internal renames with all callers in-repo (use `completing-a-cross-cutting-change`).

## Procedure
1. Keep the old path working; add the new one alongside it.
2. Emit a deprecation warning from the old path pointing to the replacement.
3. Update all in-repo callers and the docs to the new path.
4. Write a short migration note for external callers.
5. Schedule the actual removal for a later change, not this one.

## Done when
- The new path exists, the old one warns but still works, and callers have a migration route.
