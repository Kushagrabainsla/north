"""Model-availability failure classification for orchestration flows."""

from __future__ import annotations

MODEL_SCARCITY_MESSAGE = "model pool exhausted - retry when model access recovers"


class AgentFailure(str):
    """A failed agent name tagged with its classified error type.

    It remains a ``str`` so legacy failure-list consumers retain their existing
    joining, equality, and truthiness behavior.
    """

    error_type: str | None

    def __new__(cls, agent_name: str, error_type: str | None = None) -> AgentFailure:
        obj = super().__new__(cls, agent_name)
        obj.error_type = error_type
        return obj


def is_model_scarcity(failures: list[str]) -> bool:
    """Return true only when every supplied failure is model unavailability."""
    return bool(failures) and all(getattr(failure, "error_type", None) == "model_unavailable" for failure in failures)
