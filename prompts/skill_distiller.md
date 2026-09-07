You are distilling a REUSABLE SKILL from several successful software-engineering tasks that north completed. A skill is procedural memory: the concrete, repeatable steps that made this KIND of task succeed, so next time the same procedure is followed.

Here are {count} successful tasks of a similar kind:

{summaries}

Write ONE skill capturing the shared, generalizable procedure - only if there is a genuinely repeatable process here. Respond with a single JSON object:

{{
  "name": "short-kebab-case-name",
  "description": "Use when <trigger conditions>. One sentence, phrased as when to reach for this skill.",
  "body": "Numbered procedural steps; reference the specific tools/files that recur. No generic advice."
}}

Rules:
- The body must be PROCEDURAL and SPECIFIC (steps, tools, checks), not generic advice ("write clean code", etc.).
- The description must be a retrieval trigger ("Use when ..."), not a summary of what the skill is.
- If these tasks share no reusable procedure, respond exactly: {{"skill": null}}
- Output ONLY the JSON object, nothing else.
