---
name: authoring-a-north-agent
description: "Use when a user asks North to create or improve a specialist agent with its own routing scope and operating instructions."
intents: [create-agent]
domains: [general, engineering]
---
# Authoring a North agent

> North runs one agent, `general`, and grows through skills, tools and flows. A new agent is almost never the answer: it is a second router target that can take requests away from `general` and the skills it already has.

## Decide first
1. Name the gap in one sentence. Then match it:
   - know-how or a procedure → a skill (`authoring-a-north-skill`),
   - a missing action → a tool (`adding-a-north-tool`),
   - a repeatable sequence → a flow (`authoring-a-north-flow`).
2. Call `create_agent` with `action='list'` and `read` the agents that exist. If one owns the responsibility, improve it instead.
3. Create an agent only when a responsibility recurs and genuinely needs its own standing rules that would mislead `general` on other work, and you can name the requests it must not take. If you cannot name those, it is a skill.

## Write it
- **`name`**: snake_case, naming the responsibility (`tax_records_keeper`), not a person or a vague role (`assistant2`).
- **`description`**: one sentence on what it owns.
- **`accepts`**: the words and short phrases of requests it owns. Keep them specific: broad words ("help", "file", "check") pull in work that belongs to `general`.
- **`system_prompt`**: what it owns, what it does not own (and hands back), its procedure, when it asks the user, and what it reports as done. Say why behind each rule.
- Ask the user once, in one `ask_user` call, only for choices that change what it owns or may do; never guess accounts, recipients or permission to make changes.

## A compact example (a call to `create_agent`)

```yaml
action: create
name: tax_records_keeper
description: Keeps the user's tax documents filed by year and answers what is still missing for a return.
domain: general
accepts: [tax documents, tax return, form 16, 1099, w-2, receipts for taxes]
system_prompt: |
  You own the user's tax records: filing documents by tax year and saying what a return still needs.
  You do not give tax advice or file returns; say so and hand those back.
  1. Find documents with list_dir and search_files in the folder the user named; never move a file without asking.
  2. Report each tax year as a checklist: what is present, what is missing, with file paths.
  3. Ask before renaming or moving anything; report every change you made.
```

## Prove it
1. After `create_agent`, `read` it back and check it appears in `list`.
2. Route three requests it should take and three nearby ones it must not; it must take the first and leave the second to `general`.
3. Run one small real task with it. Report creation, routing and the task result separately: files existing is not an agent working.

## Common mistakes
- An agent that is really one procedure: that is a skill, and it would also need routing it does not deserve.
- `accepts` so broad that requests meant for `general` go to the new agent.
- A system prompt that repeats what every model knows instead of the rules specific to this responsibility.

## Done when
- The agent owns a distinct, recurring responsibility, routes only the requests it should, and completed one real task; or you explained why a skill, tool or flow was the better fit and built that instead.
