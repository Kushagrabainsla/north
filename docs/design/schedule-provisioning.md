# Schedule provisioning: data-owned schedules that survive update/reinstall

## Problem

The daily news briefing shipped as a code constant in `V1_CRON_ENTRIES`. That made
it an *out-of-box* schedule the user never created, yet could not remove — deleting
it only reset it to the shipped default. It also meant the schedule existed *because
the code said so*, not because it was the user's data. Removing the constant would
silently delete the schedule for everyone.

The fix the user asked for: north's schedules are **data** in `~/.north/jobs.db`
(which already survives update / reinstall). Once a schedule lives in the data, the
code seed can be removed and a north update simply picks the stored schedule up.

> **Status:** no schedule is provisioned today. A daily news briefing is one person's
> routine, so a fresh install starts with none; the mechanism below remains for a default
> north ever does want to seed, and a schedule it must run has to run a flow north ships.

## Three kinds of schedule

1. **System built-ins** — `SYSTEM_CRON_ENTRIES`. Self-maintenance north runs on
   *itself* (`task_context_cleanup`). These stay code-shipped: they are part of the
   install, not the user's list. Read-only in the UI, editable only to retime/pause
   (an edit writes an override row that `merge_entries` layers on top), and deleting
   the override restores the shipped default. Behaviour unchanged.

2. **Provisioned defaults** — `PROVISIONED_CRON_ENTRIES`. Schedules north wants a
   *fresh* install to start with (the news briefing), but which are the user's own
   the moment they exist. They are seeded into `user_cron_entries` once, then behave
   exactly like a user-created schedule: fully editable, and *deletable for good*.

3. **User schedules** — rows in `user_cron_entries` created via the API / tool.

## Provisioning ledger (the tombstone)

New table `schedule_provisioning(name TEXT PRIMARY KEY, provisioned_epoch REAL)`.

At startup `provision_default_schedules()` runs once:

- For each entry in `PROVISIONED_CRON_ENTRIES`:
  - if its name is **already in the ledger** → skip (already provisioned once; if the
    user later deleted it, it stays deleted — no resurrection).
  - else → `INSERT OR IGNORE` the row into `user_cron_entries`, then record the name
    in the ledger.

Properties:

- **Idempotent.** Runs every startup; a name is provisioned at most once, ever.
- **No resurrection.** A provisioned schedule the user deletes is not re-seeded,
  because the ledger already carries its name.
- **Scalable.** Shipping a *new* provisioned default later seeds only the new name;
  every existing one is skipped by its ledger entry. No versioning, no diffing.
- **Survives reinstall.** Both tables live in `~/.north/jobs.db` outside the install.

## Removing the code seed

Once provisioning ships, `news_daily_briefing` is removed from the code built-ins.
Upgraders: the migration writes the row (seeded from its last shipped values) before
the constant is gone, so the schedule is preserved as editable user data. Fresh
installs: the same migration seeds it on first start. Users who had deleted it (via
the override-delete path) keep it gone — see "backfill" below.

## Backfill for existing installs

An upgrader may already have an *override row* for `news_daily_briefing` (they retimed
or paused it). `provision_default_schedules()` treats an existing `user_cron_entries`
row as "already provisioned": it records the ledger entry and does not overwrite the
row. So an edited briefing keeps its edits; an unedited one is seeded fresh.
