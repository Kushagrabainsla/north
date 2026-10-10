"""Delegating coding to the coding agents installed here (Claude Code and Codex).

north does not write code. It runs the user's own agent in plan or isolated edit mode, and records the
run. See docs/design/coding-agents.md.
"""

from coding_agents.ask import Asker, Question, Reply
from coding_agents.base import (
    Briefing,
    CodingBackend,
    EventSink,
    Lander,
    LiveRun,
    RunRecorder,
    RunStart,
    Shell,
    ShellResult,
    Verifier,
    Workspaces,
)
from coding_agents.claude import ClaudeBackend
from coding_agents.codex import CodexBackend
from coding_agents.discovery import discover_backends
from coding_agents.exceptions import BackendUnavailableError, CodingAgentError
from coding_agents.gate import Decision, Gate, GateSession, GateSessions, Judge, ToolRequest, Verdict
from coding_agents.models import (
    AskAccess,
    Availability,
    Denial,
    EventKind,
    FailureKind,
    FileDelta,
    GateAccess,
    Landing,
    LandingState,
    Mode,
    Review,
    ReviewVerdict,
    RunEvent,
    RunOutcome,
    RunSpec,
    Verification,
    VerificationState,
    WorkChange,
    WorkTree,
)
from coding_agents.runner import AGENT_PREFIX, CodingRunner, RunReport
from coding_agents.verify import CommandVerifier

__all__ = [
    "AskAccess",
    "Asker",
    "Question",
    "Reply",
    "Briefing",
    "AGENT_PREFIX",
    "Availability",
    "BackendUnavailableError",
    "ClaudeBackend",
    "CodexBackend",
    "CodingAgentError",
    "CodingBackend",
    "CodingRunner",
    "CommandVerifier",
    "Decision",
    "Denial",
    "EventKind",
    "EventSink",
    "FailureKind",
    "FileDelta",
    "Gate",
    "GateAccess",
    "GateSession",
    "GateSessions",
    "Judge",
    "Lander",
    "Landing",
    "LandingState",
    "LiveRun",
    "Mode",
    "Review",
    "ReviewVerdict",
    "RunEvent",
    "RunOutcome",
    "RunRecorder",
    "RunReport",
    "RunSpec",
    "RunStart",
    "Shell",
    "ShellResult",
    "ToolRequest",
    "Verification",
    "VerificationState",
    "Verifier",
    "Verdict",
    "WorkChange",
    "WorkTree",
    "Workspaces",
    "discover_backends",
]
