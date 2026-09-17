---
name: data-and-schema-migration
description: "Use when changing a persisted schema or format - a database table, a config-file schema, or a serialized on-disk format that existing data already uses."
---
# Data and schema migration

> **Old data must still load after the change.**

## Use this when
- You are altering a DB schema, a stored file/serialization format, or a persisted config shape that live data uses.

## Do NOT use for
- Code-level API/symbol renames (use `deprecation-and-migration`).

## Procedure
1. Never mutate or drop an existing column/field in place - add new, backfill, then switch reads to it.
2. Provide an upgrade path: a migration step, or read-old / write-new logic that tolerates both shapes.
3. Make the migration idempotent and safe to re-run.
4. Test loading PRE-EXISTING data through the new code.
5. Ensure a rollback or backup exists before destructive steps.

## Done when
- Old and new data both load correctly, the migration is idempotent, and a rollback/backup exists.
