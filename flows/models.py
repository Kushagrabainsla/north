"""Value objects for declarative North flows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

FLOW_FILENAME = "FLOW.yaml"


class FlowSource(StrEnum):
    BUILTIN = "builtin"
    LEARNED = "learned"


@dataclass(frozen=True)
class FlowStep:
    """One ordered invocation of a reusable North skill.

    A flow coordinates skills; it never reaches around them to call tools.
    The skill contract selects its executor and allowed tools. ``instructions``
    specialize that procedure for this step, and ``inputs`` bind flow inputs or
    earlier outputs. A flow therefore depends only on skills.
    """

    name: str
    skill: str
    instructions: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    approval: str = "on_mutation"


@dataclass(frozen=True)
class Flow:
    """A named, ordered workflow that can later be scheduled or resumed."""

    name: str
    description: str
    steps: tuple[FlowStep, ...]
    directory: Path
    source: FlowSource = FlowSource.BUILTIN
    status: str = "active"
    domains: frozenset[str] = frozenset({"general"})
    provenance: tuple[str, ...] = ()
    # Written when a candidate is activated. The fingerprint includes every
    # referenced skill, so changing a skill invalidates the prior test evidence.
    activation_fingerprint: str = ""

    def available_to(self, domain: str) -> bool:
        return self.status == "active" and domain in self.domains

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]


def flow_fingerprint(
    flow: Flow,
    skill_resolver: Callable[[str], Any] | None = None,
) -> str:
    """Hash the executable definition and the exact procedures it references."""
    skill_fingerprints: dict[str, str] = {}
    if skill_resolver is not None:
        for name in sorted({step.skill for step in flow.steps}):
            try:
                skill_fingerprints[name] = skill_resolver(name).fingerprint()
            except Exception:
                # Validation reports missing skills. Keeping a stable sentinel in
                # the hash still prevents missing/reappearing skills from sharing
                # test evidence accidentally.
                skill_fingerprints[name] = "missing"
    payload = {
        "name": flow.name,
        "description": flow.description,
        "domains": sorted(flow.domains),
        "steps": [
            {
                "name": step.name,
                "skill": step.skill,
                "instructions": step.instructions,
                "inputs": step.inputs,
                "approval": step.approval,
            }
            for step in flow.steps
        ],
        "skills": skill_fingerprints,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
