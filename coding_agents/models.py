"""Value objects for a delegated coding run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from coding_agents.constants import DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS


class Mode(StrEnum):
    """What the agent may do."""

    PLAN = "plan"  # read and answer; nothing can change
    EDIT = "edit"  # change files in an isolated worktree, every action through the gate


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
    PAUSED = "paused"  # you paused the task: the session and the copy wait for it
    CANCELLED = "cancelled"
    ERROR = "error"


# Missing resources freeze a run; they do not fail it (CODING_STYLE 13.5). Its session and copy stay.
PAUSING_FAILURES = frozenset({FailureKind.RESOURCE, FailureKind.AUTH, FailureKind.PAUSED})


@dataclass(frozen=True)
class GateAccess:
    """Where the agent's hook asks north, and the token that says which run is asking."""

    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class AskAccess:
    """Where the agent reaches `ask_north`, and the token that says which run is asking."""

    url: str
    token: str = field(repr=False)


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
    gate: GateAccess | None = None  # required for EDIT: how the agent's hook reaches north
    ask: AskAccess | None = None  # how the agent asks north a question, when it may


@dataclass(frozen=True)
class FileDelta:
    path: str
    insertions: int
    deletions: int


@dataclass(frozen=True)
class WorkTree:
    """An isolated copy of a repository on a throwaway branch."""

    path: str
    branch: str
    base_sha: str
    base: str  # the top level of the repository it was made from
    left_out: tuple[str, ...] = ()  # repositories nested in the folder, which the copy does not have

    def start_in(self, chosen: str) -> str:
        """Where in this copy the agent works: the folder of the repository the user chose."""
        folder = Path(chosen).resolve()
        return str(Path(self.path) / folder.relative_to(self.base)) if folder.is_relative_to(self.base) else self.path


@dataclass(frozen=True)
class WorkChange:
    """What an edit run left on its branch. Nothing here has been applied to the real tree."""

    tree: WorkTree
    files: tuple[FileDelta, ...]
    # The unified diff itself, which is what applying puts in the working tree. The agent's own account of
    # its edit can differ from it: Claude Code reported `"Hello, {name}!"` and the branch held `"Hello,{name}!"`.
    diff: str = ""

    def diff_shown(self, limit: int = 8_000) -> str:
        """The diff, cut to *limit* characters with a note saying how much more there is."""
        if len(self.diff) <= limit:
            return self.diff
        return f"{self.diff[:limit]}\n[... {len(self.diff) - limit} more characters of diff]"

    @property
    def insertions(self) -> int:
        return sum(file.insertions for file in self.files)

    @property
    def deletions(self) -> int:
        return sum(file.deletions for file in self.files)


class VerificationState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # nothing to run, or it could not be run
    DECLINED = "declined"  # the user would not let the tests run


@dataclass(frozen=True)
class Verification:
    """What north found when it ran the project's own tests on the agent's changes, itself."""

    state: VerificationState
    command: str = ""
    detail: str = ""  # the failing output's tail, or why it was skipped


class ReviewVerdict(StrEnum):
    OK = "ok"
    CONCERNS = "concerns"
    UNCLEAR = "unclear"  # the reviewer did not give a verdict north could read, or could not finish


@dataclass(frozen=True)
class Review:
    """Another coding agent's read of the change. Advice, not a check: it never lands or blocks anything."""

    reviewer: str
    verdict: ReviewVerdict
    summary: str


class LandingState(StrEnum):
    APPLIED = "applied"  # the changes are in the working tree, uncommitted
    CONFLICT = "conflict"  # the same lines changed there; left on the branch
    DECLINED = "declined"  # the user kept it on the branch
    KEPT = "kept"  # not offered: the tests failed, or north's policy refused


@dataclass(frozen=True)
class Landing:
    state: LandingState
    reason: str = ""
    by_policy: bool = False  # kept because north's own policy refused it, not because of the tests


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
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class Availability:
    available: bool
    version: str = ""
    reason: str = ""  # why not, in words a person can act on
