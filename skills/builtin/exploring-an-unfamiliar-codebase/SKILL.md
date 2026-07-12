---
name: exploring-an-unfamiliar-codebase
description: "Use when you must modify a repository or module you do not yet understand, before editing unfamiliar code."
---
# Exploring an unfamiliar codebase

> **Learn the intended behaviour from the tests before you change it.**

## Use this when
- You are about to edit a repo/module whose conventions and structure you do not already know.

## Do NOT use for
- Code you already understand, or a general research question (use the researcher).

## Procedure
1. Find the entry points and the module's public surface (exports, main, routes, CLI).
2. Search for the specific symbol/feature you will touch.
3. READ its existing tests - they document intended behaviour and edge cases.
4. Trace one representative path end to end.
5. Note the project's conventions (error handling, naming, structure, style) and follow them.
6. Only then plan the edit.

## Done when
- You can state what the code does, how it is tested, and which conventions your change must follow.
