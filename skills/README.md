# Skills

A **skill** is a reusable procedure north gives an agent *before* it acts, so the
same model repeats a known-good approach instead of improvising. Skills are
advisory in ordinary tasks. A flow may invoke a skill only when it also declares
a server-enforced `execution` contract.

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

An executable skill adds this frontmatter:

```yaml
execution:
  agent: general
  tools: [browser]
  approval: on_mutation
  inputs:
    type: object
    properties:
      url: {type: string}
    required: [url]
  outputs:
    type: object
    properties:
      evidence: {type: string}
    required: [evidence]
  success_criteria:
    - The requested page was verified.
```

The contract owns the executor, exact tool allowlist, I/O schemas, minimum
approval, and success checks. A flow stores none of those choices; it references
the skill and binds step inputs. Runtime tool discovery cannot widen the allowlist.

## How it works

- **Built-in** skills ship under `resources/builtin-skills/`; **learned** skills live in `~/.north/skills/`.
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

Create `resources/builtin-skills/<name>/SKILL.md` with a trigger `description` and a short,
**procedural** body (steps, not principles). Keep it distinct from other skills so
selection stays crisp. Don't restate what the agent prompts already say. Add a
`domains: [general]` frontmatter line if the skill serves the general assistant
rather than the engineering agents (the default). Add a complete `execution`
block only when the skill is intended for flows; malformed or incomplete
contracts are rejected instead of degrading to advisory behavior.

## Measuring value

Run the scoreboard with skills off vs on — see [`evals/README.md`](../evals/README.md).
