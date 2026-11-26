# skills

A skill is a procedure that north gives an agent before the agent acts. With it, the same model
repeats a known good method and does not make one up. In an ordinary task a skill is advice.
A flow step can use a skill only when the skill has an `execution` contract, which north enforces.

## The skill format

Each skill is a folder with a `SKILL.md` file: YAML frontmatter, then a markdown body.

```md
---
name: systematic-debugging
description: Use when a reproducible bug or test failure has an unknown cause...
domains: [engineering]
intents: [debug]
---
# Systematic debugging
## Use this when / ## Do NOT use for / ## Procedure / ## Done when
```

| Field | Meaning |
|---|---|
| `name` | The skill's name. The default is the folder name. |
| `description` | The retrieval key. Write it as a trigger ("Use when ..."). |
| `domains` | Who gets the skill. The default is `[engineering]`. |
| `intents` | Optional kinds of work, from `SkillIntent` in `models.py` (for example `debug`, `create-flow`). |
| `status` | `active` (the default), `candidate` or `retired`. Only an active skill is offered. |
| `version`, `provenance` | The version, and the task IDs a learned skill came from. |
| `execution` | Optional contract for flows (see below). |

An executable skill adds this frontmatter:

```yaml
execution:
  tools: [browser]
  approval: on_mutation        # never | on_mutation | before_step
  inputs:
    type: object
    properties:
      url: {type: string}
    required: [url]
  outputs:
    type: object
    properties:
      file: {type: string}
    required: [file]
  success_criteria:
    - The requested page was verified.
  checks:
    - saved_file: {field: file}
```

The contract owns the exact tool list, the input and output schemas, the minimum approval, and
the success criteria. `success_criteria` must have at least one item. `checks` names checks that
north runs in code on the step's output (`checks.py`). A learned skill can ask for a check, but
it cannot bring code of its own. The `agent` field still loads, but north does not read it.
A flow stores none of these choices. It names the skill and binds the step inputs.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `models.py` | `Skill`, `SkillExecution`, `SkillSource`, `SkillIntent`, `SKILL_FILENAME` | The skill types. |
| `parser.py` | `parse_skill_document` | Splits a `SKILL.md` into frontmatter and body. |
| `registry.py` | `SkillRegistry`, `skill_from_document`, `parse_execution_contract`, `rejection_reason` | Loads the built-in and learned folders. Skips a bad skill with a warning. |
| `selector.py` | `SkillSelector` | Ranks skills against a task by embedding similarity. |
| `checks.py` | `parse_checks`, `run_checks`, `CHECKS` | The code checks a contract can name: `saved_file`, `arxiv_papers`. |
| `distiller.py` | `SkillDistiller` | Writes learned skills from recurring successful tasks. |
| `retirement.py` | `sweep`, `skills_to_retire`, `retire` | Marks a learned skill `retired` when its tasks keep failing. |
| `exceptions.py` | `SkillError`, `SkillParseError` | Errors for this package. |

## How north uses it

- Built-in skills ship in `resources/builtin-skills/`. Learned skills live in `skills/` in north's
  home (`~/.north`). A learned skill replaces a built-in skill with the same name.
- `agents/base.py` offers skills to north's agent. The agent's domain is `general`, so it gets
  only skills tagged `general`. The best matches (similarity 0.70 or more) appear as one-line
  descriptions. The agent calls `use_skill` to get a full body. The other names are listed too.
- A description costs about 50 tokens. A body costs about 1,500, and the agent loop sends the
  opening block again on each turn. Thus north offers descriptions, not bodies.
- `orchestrator/coding_briefing.py` gives the installed coding agents up to 2 matching
  `engineering` skills, with their bodies, in the brief before a coding run.
- Each offer in `agents/base.py` writes a `skill_selected` event and ledger entry.
- `flows/validation.py` and `tools/universal/flow_runner.py` use the `execution` contract.
- `tools/universal/create_skill.py` creates and changes skills, through `policies/self_edit.py`.

## Selection

- `SkillSelector.select` returns one primary skill and at most one more skill.
  The second skill must be different enough from the first (similarity 0.82 or less).
- A skill must reach a minimum similarity. The default is 0.35; a caller can set its own.
- If the task's words show an intent, only skills with that intent stay in the list.
- The word "north" is removed from the match text, because it says nothing about the procedure.
- If no embedding model works, the selector returns nothing.

## Learned skills

- `SkillDistiller` runs every 15 minutes when north's background work is on. It groups similar
  successful `engineering` episodes. A group of 2 or more becomes a new learned skill.
- It skips a group that already made a skill. It stops at 24 learned skills.
- A learned body longer than 8,000 characters, or one that names a path in a home folder,
  is refused. Built-in skills do not have these two limits.
- The nightly cleanup in `orchestrator/app.py` calls `retirement.sweep`. Retiring only
  changes the `status` field, so you can undo it in the file. It never deletes a skill.

## Adding a built-in skill

1. Create `resources/builtin-skills/<name>/SKILL.md`.
2. Write a trigger `description` and a short body of steps, not principles.
3. Keep it different from other skills, so that selection stays clear.
4. Do not repeat what the agent prompts already say.
5. If the skill is for north's own agent, add `domains: [general]`.
6. If the skill is for flows, add a complete `execution` block. A bad or incomplete contract
   makes the registry skip the skill.

## Measuring value

Run the scoreboard with skills off and on. See [`evals/README.md`](../evals/README.md).
