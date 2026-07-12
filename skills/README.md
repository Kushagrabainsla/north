# Skills

A **skill** is a reusable procedure north gives an engineering agent *before* it acts,
so the same model repeats a known-good approach instead of improvising.

Each skill is a folder with a `SKILL.md`:

```md
---
name: systematic-debugging
description: Use when a reproducible bug or test failure has an unknown cause...
---
# Systematic debugging
> **Iron Law...**
## Use this when / ## Do NOT use for / ## Procedure / ## Done when
```

The `description` is the retrieval key — write it as a trigger ("Use when …").

## How it works

- **Built-in** skills live in `skills/builtin/`; **learned** skills live in `~/.north/skills/`.
- For an engineering or research task, north embeds the prompt and injects the **top ~2** most
  similar skills (above a threshold) into the agent's context. If none fit, it injects nothing.
- A skill declares which agent **domains** it serves via a `domains:` frontmatter list
  (defaults to `[engineering]`); the general assistant only ever sees skills tagged `general`
  (e.g. `conducting-a-literature-review`), and engineering skills never leak into ordinary chat.
- Weak models don't need to ask: the right skill is already there. `use_skill` is a
  fallback for the long tail. Every selection is logged as a `skill_selected` ledger entry.

## Learned skills (procedural memory)

`SkillDistiller` runs in the background: it clusters north's own **recurring successful**
engineering tasks and distils the shared procedure into a new learned skill. It's
idempotent (a cluster that already produced a skill is skipped) and capped.

## Adding a built-in skill

Create `skills/builtin/<name>/SKILL.md` with a trigger `description` and a short,
**procedural** body (steps, not principles). Keep it distinct from other skills so
selection stays crisp. Don't restate what the agent prompts already say. Add a
`domains: [general]` frontmatter line if the skill serves the general assistant
rather than the engineering agents (the default).

## Measuring value

Run the scoreboard with skills off vs on — see [`evals/README.md`](../evals/README.md).
