"""Models for the agent layer. See README Section 7 and docs/CODING_STYLE.md Section 15."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from context.base import ContextStore
from inference.base import InferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry


class AgentPayload(BaseModel):
    """Input handed to an agent's `run()`. The Orchestrator constructs this."""

    task_id: str
    prompt: str
    context: str = ""  # optional pre-loaded context summary


class AgentResult(BaseModel):
    """Output of an agent run. The Orchestrator routes this to the Approval Layer."""

    output: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    has_question: bool = False
    question: str | None = None
    question_options: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Declarative agent configuration loaded from `agents/<name>/config.yaml`.

    Schema mirrors README Section 7.2. `class_name` defaults to `<TitleCase>Agent`
    when omitted in YAML.
    """

    agent: str
    domain: str
    model_pool: str = "fast_cheap"
    similar_to: str | None = None
    accepts: list[str] = Field(default_factory=list)
    output_format: str = "structured_json"
    version: str = "1.0.0"
    class_name: str | None = None

    @property
    def resolved_class_name(self) -> str:
        if self.class_name is not None:
            return self.class_name
        return f"{self.agent.capitalize()}Agent"

    @classmethod
    def from_yaml(cls, path: Path) -> "AgentConfig":
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


@dataclass
class AgentDependencies:
    """Bundle of shared dependencies injected into every agent at construction.

    Wired once by the Orchestrator (`config/dependencies.py` when that lands).
    Lets the agent ABC stay parameterless beyond `(config, deps)`.
    """

    context_store: ContextStore
    inference_router: InferenceRouter
    tool_registry: ToolRegistry
    confidence_tracker: ConfidenceTracker
