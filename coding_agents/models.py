"""Value objects for a delegated coding run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from coding_agents.constants import DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS


class Mode(StrEnum):
    """What the agent may do. Only reading and planning exist so far."""

    PLAN = "plan"


class EventKind(StrEnum):
    STARTED = "started"  # the process exists: carries its pid
    INIT = "init"  # the agent is ready: its model and session
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"  # only failures are reported
    RETRY = "retry"  # the vendor is retrying a failed API call


class FailureKind(StrEnum):
    """Why a run did not finish, because each reads differently to a person."""

    SESSION_LOST = "session_lost"  # the session to resume is gone; never start over silently
    RESOURCE = "resource"  # rate limit, overload or credit: wait, do not fail the task
    AUTH = "auth"  # the agent is logged out
    CONFIG = "config"  # a model or setting the agent does not accept
    LIMIT = "limit"  # ran out of turns, budget or time
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class RunSpec:
    """Everything one run needs."""

    task: str
    workspace: str
    session_id: str  # made by north and saved before the process starts
    mode: Mode = Mode.PLAN
    resume: bool = False
    guidance: str = ""  # the repo's own instructions, handed over as untrusted data
    max_turns: int = DEFAULT_MAX_TURNS
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD
    model: str | None = None


@dataclass(frozen=True)
class RunEvent:
    kind: EventKind
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Denial:
    """Something the agent tried that was refused."""

    tool: str
    detail: str


@dataclass(frozen=True)
class RunOutcome:
    ok: bool
    text: str
    session_id: str
    cost_usd: float = 0.0
    turns: int = 0
    denials: tuple[Denial, ...] = ()
    failure: FailureKind | None = None
    error: str = ""


@dataclass(frozen=True)
class Availability:
    available: bool
    version: str = ""
    reason: str = ""  # why not, in words a person can act on
