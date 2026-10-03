"""Delegating coding to the coding agents installed here (Claude Code now, Codex next).

north does not write code. It runs the user's own agent, read-only for now, and records the
run. See docs/design/coding-agents.md.
"""

from coding_agents.base import CodingBackend, EventSink, LiveRun, RunRecorder, RunStart
from coding_agents.claude import ClaudeBackend
from coding_agents.discovery import discover_backends
from coding_agents.exceptions import BackendUnavailableError, CodingAgentError
from coding_agents.models import (
    Availability,
    Denial,
    EventKind,
    FailureKind,
    Mode,
    RunEvent,
    RunOutcome,
    RunSpec,
)
from coding_agents.runner import AGENT_PREFIX, CodingRunner, RunReport

__all__ = [
    "AGENT_PREFIX",
    "Availability",
    "BackendUnavailableError",
    "ClaudeBackend",
    "CodingAgentError",
    "CodingBackend",
    "CodingRunner",
    "Denial",
    "EventKind",
    "EventSink",
    "FailureKind",
    "LiveRun",
    "Mode",
    "RunEvent",
    "RunOutcome",
    "RunRecorder",
    "RunReport",
    "RunSpec",
    "RunStart",
    "discover_backends",
]
