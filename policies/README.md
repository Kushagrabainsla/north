# Policies

Authoritative, always-on operating rules injected into agent **system prompts**.
A policy is north's fourth capability primitive - see the taxonomy in
`docs/ARCHITECTURE.md` §2.1 (tool / skill / policy / agent).

A **policy** binds; a **skill** advises. Use a policy for a cross-cutting rule that
must *always* hold (safety, clean-code); use a skill for procedural knowledge that
is only *sometimes* relevant.

## Format

Each policy is a markdown file with YAML frontmatter:

```md
---
applies_to: "*"                 # every agent, OR a list: [coder, reviewer]
---
## Title
The authoritative rule text...
```

- `applies_to: "*"` binds every agent; a list binds only those agents (by name).
- The body is injected verbatim into the system prompt of each matching agent, on
  every run, via `agents/policy.py` (`load_policies` + `render_policies`).

## Fail closed

These are core guardrails, so loading **fails closed**: a missing directory, an
empty directory, or any malformed/empty policy raises `PolicyError` at import - a
typo can never silently drop the safety rules. `README.md` is ignored.

## Enforcement

A policy is the instruction layer; a weak model can still miss it. The critical
rules are therefore also enforced deterministically in code (approval-gated
mutating tools, the Definition-of-Done gate). Prefer code enforcement for anything
that can be enforced in code.

## Shipped policies

- `safety.md` (`*`) - report tool failures honestly, never fabricate, confirm
  before irreversible/external actions.
- `clean-code.md` (`coder`, `reviewer`) - the clean-code standard the coder applies
  and the reviewer enforces.
