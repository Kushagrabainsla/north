---
name: authoring-a-north-flow
description: "Use when a user asks North to create, automate, schedule, or improve a reusable multi-step flow made from skill invocations."
intents: [create-flow]
domains: [general, engineering]
---
# Authoring a North flow

> A flow is an ordered process north can run again and again: each step runs one skill. The user's outcome is the goal; writing the flow is one setup step, and it is done when the flow has run once successfully.

## Decide first
1. Restate what each run should achieve and how the user will know it worked.
2. Look before building: `create_flow` and `create_skill` with `action='list'`, and `list_schedules`. Reuse or improve what exists. Steps run skills; a skill uses tools. A missing procedure is a skill to write first (`authoring-a-north-skill`); a missing action is a tool (`adding-a-north-tool`).

## Settle the setup, then ask once
Complete the setup checklist below before calling `create_flow:create`. Find out technical facts yourself; the user should only decide what is theirs to decide. Then ask every unresolved choice in **one** `ask_user` call, as a short numbered list, so setup takes one exchange, not five. Keep earlier answers and do not ask again for details already supplied. If the user answers with a question, answer it and ask the still-open choices again in the same run: a clarification is not completion.

### Setup checklist
- **Outcome and evidence**: what each run achieves and what proves it.
- **Inputs and preferences**: the sources, files, accounts and filters it needs; which are saved choices and which come with each run. Never invent personal facts; an open per-run value can stay a required input if the user chose that.
- **Outputs and delivery**: what it produces, where it goes, and whether it is shown in chat, saved to files, or sent somewhere the user allowed.
- **Scope and safety**: how much per run, how duplicates are handled, what needs the user's review, and what must never happen.
- **Timing**: a required choice even when the request only says "create a flow". If it was not supplied, call `ask_user`: "When should this flow run?" offering "Manual only", "Once at a specific time", and "Recurring" (in the same question as any other open choice). Do not silently default to manual, and do not read "automate" as daily.
  - Once at a specific time needs the date and local time; recurring needs a frequency or interval, or the days and local time. "Recurring", "daily" or "in the morning" alone needs a follow-up. Never invent 09:00 or weekdays.
  - The timezone is the one configured in North settings, given in the runtime context. The timezone is not a flow input or question: Do not ask for timezone, declare a timezone input, or pass a per-schedule override. Name it in the summary as information. If settings cannot be read, report that rather than guessing.
  - "Leave it a draft" or "do not install a schedule yet" does not answer when the finished flow should run.
- Before authoring, summarize the agreed outcome, inputs, outputs, limits, safety boundary and timing. Choosing timing is not approval to test, activate, or install it.

### Browser work
Select an enabled `profile_id` for each browser tool call from the saved profiles, using the task and each profile's purpose. If that leaves it unclear, ask which browser profile to use. Profiles belong to tool calls, never to flow definitions or inputs. Never silently switch profiles or copy cookies as a fallback. If the site shows the expected account already signed in, reuse that session; saved passwords alone do not prove a login. Ask for sign-in help only when the site needs it (expired session, MFA, a locked password manager), with `ask_user` and `requires_user_action=true` as its own call, offering "Done". Never read or export passwords or ask for them in chat. After any answer, inspect and assert the expected account or state on the page before continuing: an automatic answer is not proof of login. A connection failure is a setup problem, not a login problem.

## Build it
- Every step names a `skill` with an execution contract, gives concrete `instructions`, and binds its `inputs` (`${inputs.folder}` takes a value from the run's inputs). Agents and tools belong to the skill's contract, never to a step.
- Safety is the step's `approval`, because a sentence in the instructions does not stop a tool: `never` refuses changes, `on_mutation` asks before each change, `before_step` asks once before the step starts. A step's approval can be stricter than its skill's minimum, never looser.
- Bound every step: limits, timeouts, and duplicate handling. Never "all" without a maximum.
- Keep user-specific paths and preferences in inputs, not in the steps.

## A complete example (a call to `create_flow`)

```yaml
action: create
name: weekly-reading-digest
description: Every Sunday evening, a digest of the articles saved in the Reading folder that week.
steps:
  - name: digest
    skill: summarizing-saved-articles
    instructions: Summarize the articles saved in the last 7 days, newest first, and write the digest file.
    inputs:
      folder: ${inputs.folder}
      digest_path: ${inputs.digest_path}
    approval: on_mutation
test_inputs:
  folder: ~/Reading
  digest_path: ~/Reading/digests/test-digest.md
```

## Test, switch on, schedule
1. Call `create_flow` (or `update` when a flow already owns the process) with `test_inputs` for the smallest safe case. The user is asked once; the flow then runs once as a real test (each step still asks as its approval says) and is live if it completes. A failing new flow stays a draft; a failing edit goes back to the live version. Read the error, fix it, call again. A draft that already exists goes live with `action='activate'`.
2. Check the test run's record, not just the call's return: the expected output exists, nothing prohibited happened.
3. Only when the flow is live and the user confirmed installation, create the schedule with `schedule_task`, read it back, and check its next run. Manual-only flows need none. If installation was deferred, report the timing as pending, not installed.
4. When updating a flow, read its definition and `list_schedules` first, and preserve existing timing (cadence and local clock time) unless the user asks to change it.

## Report each part separately
Live or still a draft (and why), environment checked, schedule installed (with its next run) or pending, and the first successful run. `create_flow:list` or `read` proves only that you looked; only a successful `create`, `update` or `activate` makes a flow live.

## Common mistakes
- Five separate questions where one numbered question would do.
- A flow whose step names a tool or an agent directly, or a skill without an execution contract.
- A safety rule written only in the instructions ("do not submit") instead of the step's `approval`.
- Installing a schedule before the flow is live, or treating chosen timing as permission to install it.
- A separate `request_approval` for the create or update: `create_flow` asks the user itself.
- A user's custom process added as a built-in or a repository edit: it belongs in learned skills and flows created with `create_skill` and `create_flow`.

## Done when
- The setup choices, including timing, were settled and summarized without asking twice; existing capabilities were reused; the flow passed its test and is live; any requested schedule exists. If north lacks something the flow needs, stop at a draft and name it plainly.
