---
name: authoring-a-north-flow
description: "Use when a user asks North to create, automate, schedule, or improve a reusable multi-step flow made from skill invocations."
intents: [create-flow]
domains: [general, engineering]
---
# Authoring a North flow

> A flow is an executable, ordered process. The user's outcome is the goal; creating YAML is only one setup step.

## Procedure
1. Restate the requested outcome and completion evidence. Separate the trigger, ordered work, user-review boundary, and any prohibited final action.
2. Search existing flows, skills, tools, agents, and schedules. Reuse or compose them before creating another entity. Flows invoke skills; skills may call several atomic tools. If a procedure is missing, create or improve the skill. If an atomic capability is missing, identify that tool gap first.
3. Ask only for choices North cannot safely discover. For browser work, ask existing CDP browser versus isolated browser, explain access to sessions/cookies/tabs/extensions, and run a login and dependency preflight.
   - If the user replies with a question instead of resolving the choice, answer it and ask the unresolved choice again in the same run. A clarification is not completion.
4. Check feasibility against the live capability catalog. Every step must select one active skill with an execution contract, give concrete instructions, and bind its inputs. The skill contract owns the executor and exact tool allowlist. Never put an agent or tool directly in a flow step.
5. Define bounded behavior in each skill contract: inputs, outputs, allowed tools, limits, timeouts, retries, persistent deduplication, minimum approval, and clear success criteria. Never use "all" without a safe maximum.
6. Encode safety as executable policy. `never` blocks mutating tool calls, `on_mutation` asks immediately before each mutating tool call, and `always` asks before the skill step begins. "Do not submit" in prose is insufficient.
7. Call `create_flow` to create a candidate, or update the existing flow when it already owns the process. Every update returns the flow to candidate status. Read it back and call `create_flow` with `action=validate`. Fix every error rather than explaining it away.
8. Run the candidate once with `run_flow` in test mode on the smallest safe case. Test mode executes real tools and is not a dry run, so keep approval boundaries intact. Verify expected output, prohibited actions, state persistence, and failure behavior. Do not install a schedule before this passes.
9. Activate only with the completed test run as evidence and explicit user confirmation. If recurring work was requested, create the schedule, read it back, and verify its next firing.
10. Report each state separately: candidate created, validation passed, environment verified, test passed, activated, schedule installed, and first successful run. Green means the requested automation is operational, not that a file exists.
11. Treat tool actions as evidence, not tool names. `create_flow:list` or `create_flow:read` proves discovery only; creation requires `create_flow:create` or `create_flow:update`, validation requires `create_flow:validate`, and activation requires `create_flow:activate`.

## Done when
- Existing capabilities were reused where possible, every referenced skill contract validates, a bounded test passed, activation is evidence-backed, and any requested schedule exists.
- If North lacks a required runtime feature, stop at a candidate and name the missing capability plainly.
