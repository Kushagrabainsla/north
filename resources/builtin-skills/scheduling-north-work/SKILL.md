---
name: scheduling-north-work
description: "Use when a user asks North to run a task later or repeatedly through a one-shot, wall-clock, or fixed-interval schedule."
intents: [create-schedule]
domains: [general, engineering]
---
# Scheduling North work

> A schedule is a trigger, not the capability that performs the work. Schedule only an operational task, skill, or flow.

## Procedure
1. Identify what must run, when it must run, the user's timezone, and what result or notification proves each run completed.
2. Inspect existing schedules and reusable flows or skills. Update the schedule that already owns the job instead of creating a duplicate.
3. Choose exactly one timing model: a one-shot local datetime, a fixed interval in minutes, or a recurring wall-clock time with optional weekdays.
4. Verify every referenced agent, skill, flow, tool, credential, and external environment exists. Skills and flows must be active, not candidates.
5. For browser work, ask isolated browser versus the user's existing CDP browser before scheduling. Explain that the existing browser can expose sessions, cookies, tabs, and extensions, then verify login and connection while the user is present.
6. Put bounded work in the scheduled prompt: limits, deduplication key, retry behavior, per-item failures, approval boundary, and prohibited final actions. A timer must never turn an unsafe action into unattended authority.
7. Create or update the schedule, then list it back and verify its label, payload, timing mode, timezone, enabled state, and next firing.
8. When practical, run the underlying task once safely before relying on the timer. Distinguish “schedule installed” from “first scheduled run completed.”
9. Report the next local run time and how the user can pause, edit, or remove the schedule.

## Done when
- The underlying capability is operational, one unambiguous trigger is stored, the next firing is correct in the user's timezone, and safety and completion evidence are explicit.
