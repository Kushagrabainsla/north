"""Models for the agent layer. See README Section 7 and docs/CODING_STYLE.md Section 15."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field

from inference.base import InferenceRouter
from memory.base import ContextStore
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry
from utils.ids import generate_id

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from ledger.base import LedgerWriter
    from memory import MemoryGateway
    from memory.facts import FactStore
    from tools.tool_index import ToolIndex


@runtime_checkable
class StreamEmitter(Protocol):
    """Structural protocol satisfied by EventStreamManager - avoids circular imports."""

    async def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None: ...


class AgentPayload(BaseModel):
    """Input handed to an agent's `run()`. The Orchestrator constructs this."""

    task_id: str
    # One identity per actual invocation. Delegated agents and retries receive a
    # fresh id while remaining linked to the same top-level task.
    run_id: str = Field(default_factory=generate_id)
    parent_run_id: str | None = None
    attempt: int = 0
    prompt: str
    context: str = ""  # optional pre-loaded context summary
    workspace: str = ""  # root directory for filesystem/shell tools
    model_pool: str = "reasoning"  # task-scoped model pool (reasoning, speed, fast_cheap, vision)
    delegation_depth: int = 0  # incremented on each delegate_task call; capped at MAX_DELEGATION_DEPTH
    delegation_chain: list[str] = Field(default_factory=list)  # ordered agent names in this call chain
    # Model ids this run must avoid, so the agent is forced onto a different model
    # (e.g. a reviewer excluding the coder's model). Threaded into every inference
    # call the agent makes. Empty = no constraint.
    exclude_models: list[str] = Field(default_factory=list)
    # When False, delegate_task is neither offered to the model nor executed for this
    # run. The conductor sets this on the reviewer so it reports only and never
    # delegates a fix back to the coder - the orchestrator owns that fix loop, and a
    # second delegation path would duplicate work and bypass the bounded cap.
    allow_delegation: bool = True


class AgentResult(BaseModel):
    """Output of an agent run. The Orchestrator routes this to the Approval Layer."""

    output: str
    summary: str
    run_id: str | None = None
    parent_run_id: str | None = None
    attempt: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    has_question: bool = False
    question: str | None = None
    question_options: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0  # summed prompt tokens across the agent's LLM calls
    tokens_out: int = 0  # summed completion tokens across the agent's LLM calls
    duration_ms: int | None = None
    tools_used: list[str] = Field(default_factory=list)  # deduplicated, ordered by first call
    # Tools that succeeded at least once, deduplicated and ordered by first success.
    # Evidence for claims-vs-output verification (orchestrator/verification.py).
    # None means the agent has no tool loop, so its output is not verifiable this way.
    successful_tools: list[str] | None = None
    # Model id(s) the agent actually used across its run, in first-seen order.
    # Recorded so downstream logic can prove/audit which model produced this result
    # (e.g. enforcing that a reviewer used a different model than the coder) and so
    # the ledger's model_used column reflects reality. Empty when unknown.
    models_used: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Declarative agent configuration loaded from `agents/<name>/config.yaml`.

    Schema mirrors README Section 7.2. `class_name` defaults to `<TitleCase>Agent`
    when omitted in YAML.
    """

    agent: str
    domain: str
    model_pool: str | None = None
    similar_to: str | None = None
    accepts: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    output_format: str = "structured_json"
    version: str = "1.0.0"
    class_name: str | None = None
    # Agent names this agent must NOT share a model with (Copilot /subagents style).
    # The orchestrator translates this into per-run exclude_models so, e.g., the
    # reviewer is a genuine second opinion on a different model than the coder.
    distinct_from: list[str] = Field(default_factory=list)

    @property
    def resolved_class_name(self) -> str:
        if self.class_name is not None:
            return self.class_name
        return "".join(word.capitalize() for word in self.agent.split("_")) + "Agent"

    @classmethod
    def from_yaml(cls, path: Path) -> AgentConfig:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


@dataclass
class AgentDependencies:
    """Bundle of shared dependencies injected into every agent at construction.

    Wired once at startup via ``config/dependencies.py`` and ``app.py``.
    Lets the agent ABC stay parameterless beyond ``(config, deps)``.
    """

    context_store: ContextStore
    inference_router: InferenceRouter
    tool_registry: ToolRegistry
    confidence_tracker: ConfidenceTracker
    stream_manager: StreamEmitter | None = field(default=None)
    episodic_store: Any | None = field(default=None)
    # Injected after construction to break the circular dependency:
    # agent_registry → agent_deps → agent_registry.
    agent_registry: Any | None = field(default=None)
    # Required for the request_approval tool.  Must be the same ApprovalStore
    # instance used by the Orchestrator so waits and resolutions are consistent.
    approval_store: ApprovalStore | None = field(default=None)
    # Optional - when set, request_approval checks learned judgement rules first
    # and skips the user prompt when a rule fires at high confidence.
    # Injected after construction (same pattern as agent_registry) to avoid
    # building it twice.
    judgement_filter: JudgementFilter | None = field(default=None)
    # Optional - when set, surfaced cards also fire a system alert (macOS/terminal)
    # via the TUI-aware Notifier so approvals reach the user when no TUI is attached.
    notifier: Notifier | None = field(default=None)
    # Semantic tool selection: top-K relevant tools injected per task instead
    # of the full registry list.  None → fall back to full injection.
    tool_index: ToolIndex | None = field(default=None)
    # Semantic context retrieval: per-fact embeddings instead of full doc load.
    # None → fall back to full markdown document load.
    fact_store: FactStore | None = field(default=None)
    # Single gated memory interface. When set, agents read all context through
    # it; when None, a gateway is built on the fly from the stores above so the
    # gate still applies. See memory/gateway.py.
    memory: MemoryGateway | None = field(default=None)
    # Optional ledger writer for recording delegation failures from agents.
    # Injected at startup; None in tests that do not require audit trail.
    ledger: LedgerWriter | None = field(default=None)
    # Iteration caps injected from Settings so agents never read config globals.
    agent_max_iterations: int = 40
    agent_history_keep_recent: int = 10
    # Approval wait timeout injected from NorthSettings so it is configurable
    # without touching source code.
    approval_timeout_seconds: float = 300.0
    # Optional in-flight registry. When set, the agent marks the task as having
    # performed side effects the first time a mutating tool succeeds, so an
    # interrupted task that already acted is not blindly re-run on restart.
    running_task_store: Any | None = field(default=None)
    # Optional task-scoped plan/TODO store (#9 plan-and-track). When set, agents
    # can maintain a live checklist via the update_plan tool and the current plan
    # is injected into their context so long tasks stay anchored on progress.
    plan_store: Any | None = field(default=None)
    # Live user settings (NorthSettings) - lets agents read the current approval mode
    # so e.g. ask_user does not block in autonomous mode.
    north_settings: Any | None = field(default=None)
    # Skills subsystem (procedural memory). When set, engineering agents get the
    # most relevant skill(s) injected into context and can pull others via use_skill.
    # Both are optional so non-engineering setups and tests need not wire them.
    skill_registry: Any | None = field(default=None)
    skill_selector: Any | None = field(default=None)
    # Durable execution index shared by top-level and delegated agents.
    agent_run_store: Any | None = field(default=None)
