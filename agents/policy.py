"""Policies: authoritative, always-on operating rules for agents.

A **policy** is a cross-cutting rule an agent must obey - safety guardrails,
clean-code standards - and is north's fourth capability primitive alongside
tools, skills, and agents:

- **tool**   - deterministic code the model calls.
- **skill**  - advisory procedural *knowledge*, semantically selected per task and
               injected LOW (in the user-message context), never overriding instructions.
- **policy** - an authoritative *rule*, activated deterministically (by agent name),
               injected HIGH (in the system prompt), binding by design.
- **agent**  - a separate execution boundary (its own model call / persona / tools).

Policies live in the repo's ``policies/`` directory as markdown files with a YAML
frontmatter declaring which agents they bind::

    ---
    applies_to: "*"                 # every agent
    ---
    ## Safety ...

    ---
    applies_to: [coder, reviewer]   # only these agents
    ---
    ## Clean code ...

Built-in policies are core guardrails, so loading **fails closed**: a malformed or
empty policy file raises rather than silently leaving agents unprotected. (Prompt
text is the instruction layer; the critical rules are also backed by deterministic
enforcement - approval-gated mutating tools, the Definition-of-Done gate - because a
weak model can always miss an instruction.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_ALL = "*"


class PolicyError(Exception):
    """A built-in policy file is missing, malformed, or empty (fail closed)."""


@dataclass(frozen=True)
class Policy:
    """One authoritative rule set and the agents it binds."""

    name: str
    applies_to: frozenset[str] | None  # None => every agent ("*")
    body: str

    def applies(self, agent_name: str) -> bool:
        return self.applies_to is None or agent_name in self.applies_to


def _parse_policy(path: Path) -> Policy:
    """Parse one ``policies/<name>.md`` file (fail closed on any problem)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise PolicyError(f"{path.name}: must start with '---' YAML frontmatter declaring applies_to")
    # maxsplit=2 keeps any '---' inside the body intact.
    _, front, body = text.split("---", 2)
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path.name}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise PolicyError(f"{path.name}: frontmatter must be a mapping")
    body = body.strip()
    if not body:
        raise PolicyError(f"{path.name}: empty policy body")

    raw = meta.get("applies_to", _ALL)
    if raw == _ALL:
        applies_to: frozenset[str] | None = None
    elif isinstance(raw, list) and raw and all(isinstance(x, str) and x.strip() for x in raw):
        applies_to = frozenset(x.strip() for x in raw)
    else:
        raise PolicyError(f'{path.name}: applies_to must be "*" or a non-empty list of agent names')
    return Policy(name=path.stem, applies_to=applies_to, body=body)


def load_policies(policies_dir: Path) -> list[Policy]:
    """Load every built-in policy, sorted by name for deterministic order.

    Fails closed: a missing directory, an empty directory, or any malformed policy
    raises ``PolicyError`` - so a typo can never silently drop the safety guardrails.
    ``README.md`` is ignored so the directory can document itself.
    """
    if not policies_dir.is_dir():
        raise PolicyError(f"policies directory not found: {policies_dir}")
    files = [p for p in sorted(policies_dir.glob("*.md")) if p.name.lower() != "readme.md"]
    if not files:
        raise PolicyError(f"no policy files found in {policies_dir}")
    return [_parse_policy(p) for p in files]


def render_policies(policies: list[Policy], agent_name: str) -> str:
    """The authoritative policy block for *agent_name*: matching bodies concatenated.

    Returns "" when no policy applies, so it appends cleanly to a system prompt.
    """
    bodies = [p.body for p in policies if p.applies(agent_name)]
    return "\n\n" + "\n\n".join(bodies) if bodies else ""
