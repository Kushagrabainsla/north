"""Model-availability failure classification for orchestration flows."""

from __future__ import annotations

from dataclasses import dataclass

MODEL_SCARCITY_MESSAGE = "model pool exhausted - retry when model access recovers"


@dataclass(frozen=True)
class AgentFailure:
    """Why north failed a task after every retry, and the kind of failure it was.

    ``model_unavailable`` queues the task to run again when a model is back.
    """

    reason: str
    error_type: str | None = None
