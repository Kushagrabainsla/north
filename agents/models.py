"""Models for the agent layer. See README Section 7 and docs/CODING_STYLE.md Section 15."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field

from context.base import ContextStore
from inference.base import InferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from approval.store import ApprovalStore


@runtime_checkable
class StreamEmitter(Protocol):
    """Structural protocol satisfied by EventStreamManager — avoids circular imports."""

    async def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None: ...


class AgentPayload(BaseModel):
    """Input handed to an agent's `run()`. The Orchestrator constructs this."""

    task_id: str
    prompt: str
    context: str = ""  # optional pre-loaded context summary
    workspace: str = ""  # root directory for filesystem/shell tools
    delegation_depth: int = 0  # incremented on each delegate_task call; capped at _MAX_DELEGATION_DEPTH


class AgentResult(BaseModel):
    """Output of an agent run. The Orchestrator routes this to the Approval Layer."""

    output: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    has_question: bool = False
    question: str | None = None
    question_options: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0


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
    stream_manager: StreamEmitter | None = field(default=None)
    # Optional episodic memory store.  When present, _load_context() injects
    # semantically relevant past task summaries into every agent prompt.
    episodic_store: "Any | None" = field(default=None)
    # Agent registry — injected after construction to avoid circular dependency.
    # Used by AgenticLLMAgent's delegate_task tool for hierarchical execution.
    agent_registry: "Any | None" = field(default=None)
    # Injected approval store so agents don't rely on the module-level singleton.
    approval_store: "ApprovalStore | None" = field(default=None)
