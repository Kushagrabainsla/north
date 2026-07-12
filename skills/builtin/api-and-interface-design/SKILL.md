---
name: api-and-interface-design
description: "Use when designing or changing a PUBLIC interface - an API, data schema, CLI, plugin contract, or a widely-imported signature - not an internal one-off helper."
---
# API and interface design

> **Write the call sites before you finalize the signature.**

## Use this when
- You are shaping something other code will depend on: a public function/class, endpoint, schema, CLI, or plugin contract.

## Do NOT use for
- Private helpers or an internal refactor (use `safe-refactoring`).

## Procedure
1. Write at least two realistic call-site examples first; let the caller's clarity drive the design.
2. Choose names and parameters for the reader; prefer few arguments and no boolean flag args.
3. Make illegal states unrepresentable; define the error and edge behaviour.
4. Check backward compatibility against existing callers. If it breaks them, plan a migration (see `deprecation-and-migration`).
5. Keep it minimal - the smallest surface that serves the call sites.

## Done when
- The interface reads cleanly at the call sites, its failure modes are defined, and existing callers still work or have a migration.
