---
name: authoring-a-north-agent
description: "Use when a user asks North to create or improve a specialist agent with its own routing scope and operating instructions."
intents: [create-agent]
domains: [general, engineering]
---
# Authoring a North agent

> An agent is a durable specialist and routing boundary. Do not create one merely to hold a single workflow or one missing action.

## Procedure
1. Restate the recurring responsibility the agent would own and the evidence that specialization improves the user's outcome.
2. Inspect registered agents, their domains, and routing phrases. Reuse or improve an existing owner when its responsibility already overlaps.
3. Decide whether the gap is actually an agent. Use a skill for reusable judgement, a tool for one executable action, and a flow for an ordered process.
4. Define a narrow domain, positive routing examples, nearby requests it must reject, required tools and skills, authority limits, escalation behavior, and output contract.
5. Ask only for missing choices that change ownership or authority. Never infer accounts, credentials, recipients, browser profile, or permission for consequential actions.
6. Write a system prompt that names what the agent owns, what it does not own, its procedure, approval boundaries, failure behavior, and observable completion evidence.
7. Call `create_agent` only after checking the live registry. Read the generated configuration and prompt back, then verify the agent appears in the registry.
8. Route representative positive and negative requests. Confirm the new agent accepts work it owns, does not steal adjacent work, and can complete one smallest safe task with its real dependencies.
9. Report file creation, registry loading, routing checks, and task execution separately. Do not call the agent ready when only its files exist.

## Done when
- The agent has distinct ownership, bounded routing, truthful dependencies and authority, and evidence from both routing checks and a representative task.
