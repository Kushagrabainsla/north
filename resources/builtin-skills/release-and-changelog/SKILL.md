---
name: release-and-changelog
description: "Use when cutting a release - drafting the release notes / changelog from the merged changes and tagging a version."
---
# Release and changelog

> **The changelog is for humans reading later; group by impact, not by commit.**

## Use this when
- You are preparing release notes / a changelog and a version tag from merged work.

## Do NOT use for
- The mechanics of committing, pushing, or opening a PR (north's deploy flow handles that).

## Procedure
1. Collect the merged changes since the last tag.
2. Group them into Added / Changed / Fixed / Removed.
3. Write user-facing entries - the impact, not the internal detail.
4. Pick the version by semver impact.
5. Update the changelog and version, then create the tag / release notes.
6. Never auto-deploy to production without explicit human sign-off.

## Done when
- The changelog reads clearly for a user, the version is chosen, and the tag/notes are prepared.
