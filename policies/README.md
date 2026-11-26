# policies

This package holds `self_edit.py`, the server rule for the files north may write for itself.
The model can ask to change a file, but this rule decides if the change is allowed.
The prompt policies (rules in the system prompt) are a different thing with a similar name.
They live in `resources/policies/`, and the section below describes them.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `self_edit.py` | `SelfEditPolicy`, `Mutation` | Allows or refuses each create, update or activation of a north-owned file, and records it. |

`SelfEditPolicy` works inside one root folder. Its methods:

- `authorize(path, operation)` returns a refusal reason, or `None` when the change is allowed.
- `begin(path, operation)` checks the change and keeps a copy of the old file (a before-image).
- `commit(mutation)` writes the change to the authorship record, with the file's SHA-256.
- `revert(mutation_id)` puts the before-image back, or deletes a file that north created.
- `forget(directory)` removes the records for a folder that the user deleted.

The operations are `create`, `update`, `activate` and `user_update`:

- `create` is allowed only for a file that does not exist yet.
- `update` is allowed only for a file that north created or updated before.
- `activate` marks a checked skill or flow as live. It is allowed on any file that exists.
- `user_update` is a change that the user approved to a `SKILL.md` or `FLOW.yaml` file.
  Only a policy made with `user_document` allows it.

## How north uses it

- `orchestrator/app.py` makes one `SelfEditPolicy` for each managed folder in north's home:
  the learned tools, `skills/` (with `user_document="SKILL.md"`) and `flows/`
  (with `user_document="FLOW.yaml"`).
- `tools/universal/create_tool.py`, `create_skill.py` and `create_flow.py` call `begin()` and
  `commit()` around each write. `tools/universal/_procedure_edit.py` holds the shared steps.
- `web/extensions.py` calls `forget()` when the user deletes a learned tool, skill or flow.

## Rules

- Some paths are always frozen: `approval/**`, `tools/_path.py`, `ledger/**`, `inference/**`,
  `config/**`, `credentials/**`, `.env` and `.env.*`. North can never write them.
- Activation and user updates do not make a file north's own. After them, north still cannot
  `update` the file without the user.
- The records are in `mutations/` in north's home: `authorship.json` and the `before/` folder.

## Prompt policies (`resources/policies/`)

A prompt policy is a rule that is always in an agent's system prompt. A policy binds; a skill
advises. Use a policy for a rule that must always apply, such as safety. Use a skill for
knowledge that applies only to some tasks.

Each policy is a markdown file with YAML frontmatter:

```md
---
applies_to: "*"                 # every agent, OR a list of agent names: [north]
---
## Title
The rule text...
```

- `agents/policy.py` loads the files (`load_policies`) and makes the prompt block
  (`render_policies`). `agents/llm_agent.py` adds the block to each matching agent's system prompt.
- `utils/runtime_resources.py` (`policies_dir`) finds the folder in a checkout or an install.
- Loading fails closed. A missing folder, an empty folder, or a bad or empty file raises
  `PolicyError`, so a typo cannot drop the safety rules. `README.md` is ignored.
- A weak model can miss a prompt rule. Thus north also enforces the important rules in code,
  for example with approval cards on tools that change something. If you can enforce a rule
  in code, do it in code.

The shipped policy is `safety.md` (for every agent). It tells the agent to report tool failures
honestly, to never make up facts, and to get one approval for each irreversible or external action.
