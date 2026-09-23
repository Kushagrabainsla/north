---
name: authoring-a-north-skill
description: "Use when North should create or update reusable procedural instructions that teach an agent how to handle a recurring kind of request."
intents: [create-skill]
domains: [general, engineering]
---
# Authoring a North skill

> A skill supplies judgement and procedure. For flow use, its execution contract also selects the executor and exact atomic tools. It must not pretend to replace a missing tool or runtime feature.

## Procedure
1. Confirm the request is reusable know-how rather than one execution, an atomic action, or an ordered automation. Use a tool for a missing action and a flow for a repeatable sequence.
2. Search the skill catalog for an existing procedure to reuse or improve. Prefer one authoritative skill over overlapping variants that compete during selection.
3. Define the trigger in plain language: when to use the skill, when not to use it, the domains it serves, and representative prompts that should select it.
4. Write a concrete numbered procedure naming required inputs, questions, safety boundaries, failure behavior, and observable completion evidence. Never hide missing implementation behind instructions.
5. If a flow will invoke the skill, define its execution contract: one compatible agent, an exact tool allowlist, object schemas for inputs and outputs, a minimum approval mode, and verifiable success criteria. A skill may call several atomic tools; a flow must not redefine them.
6. Keep task-specific credentials, paths, account names, consent, and temporary facts out of reusable instructions. Reference capabilities by stable names.
7. Call `create_skill` to create a candidate, or update the existing skill when it already owns the procedure. Read the stored result and verify the registry loads the candidate.
8. Call `create_skill` with `action=validate`, representative positive prompts, and nearby negative prompts. Fix collisions or missed triggers. Then test that following the procedure reaches its completion condition without exceeding its authority.
9. Activate only after validation and explicit user confirmation. Report creation separately from proven usefulness. A well-formed file is not evidence that the skill is selected correctly or produces successful runs.

## Done when
- The skill has a distinct trigger, bounded scope, executable dependencies, safety rules, completion evidence, and selection tests that do not collide with existing skills.
