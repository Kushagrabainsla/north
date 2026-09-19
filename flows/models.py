"""Value objects for declarative North flows."""

from __future__ import annotations

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
    """One ordered step in a flow.

    ``tool`` names an allowlisted North tool.  ``skill`` optionally names a
    reusable body of know-how to inject before the step runs.  Execution is
    deliberately not part of this value object; the runner owns permissions,
    retries, and approval checkpoints.
    """

    name: str
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    skill: str = ""
    approval: str = "on_mutation"
    description: str = ""


@dataclass(frozen=True)
class Flow:
    """A named, ordered workflow that can later be scheduled or resumed."""

    name: str
    description: str
    steps: tuple[FlowStep, ...]
    directory: Path
    source: FlowSource = FlowSource.BUILTIN
    version: str = "1.0.0"
    status: str = "active"
    domains: frozenset[str] = frozenset({"general"})
    provenance: tuple[str, ...] = ()

    def available_to(self, domain: str) -> bool:
        return self.status == "active" and domain in self.domains

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]
