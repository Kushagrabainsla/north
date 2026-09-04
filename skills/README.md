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
- For an engineering or research task, north embeds the prompt and offers the **top ~3** most
  similar skills (above a threshold) as one-line descriptions. The agent calls `use_skill` to
  pull the full procedure when one matches. If none fit, it offers nothing.
- Descriptions, not bodies, is deliberate: the opening block of an agent conversation is
  re-sent on every turn, so a pasted playbook is paid ~20 times per task whether it is used
  or not. A description costs ~50 tokens against ~1,500 for two bodies.
- A skill declares which agent **domains** it serves via a `domains:` frontmatter list
  (defaults to `[engineering]`); the general assistant only ever sees skills tagged `general`
  (e.g. `conducting-a-literature-review`), and engineering skills never leak into ordinary chat.
- `use_skill` is now the normal way a skill is loaded, not just the long-tail fallback -
  the agent is told which skills fit and fetches the one it wants. Every selection is
  logged as a `skill_selected` ledger entry.

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
