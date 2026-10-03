"""What a coding backend is, and what north gives the runner to record a run.

The runner never imports north's run store or approval layer; the composition root hands it
implementations of the small protocols here (CODING_STYLE 6.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from coding_agents.models import (
    Availability,
    Landing,
    Review,
    RunEvent,
    RunOutcome,
    RunSpec,
    Verification,
    WorkChange,
    WorkTree,
)

EventSink = Callable[[RunEvent], Awaitable[None]]


class CodingBackend(ABC):
    """One installed coding agent that north can run."""

    name: str
    # The key this backend's session state is stored under in a run's provider state.
    provider: str

    @abstractmethod
    async def probe(self) -> Availability:
        """Whether this agent can take a run now: installed, new enough, logged in."""

    @abstractmethod
    async def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        """Run *spec* to its end, reporting events as they happen.

        Cancelling the awaiting task stops the agent and everything it started.
        """


@dataclass(frozen=True)
class LiveRun:
    """An earlier run of the same task that can continue."""

    run_id: str
    session_id: str
    worktree: WorkTree | None = None  # an edit run continues in the copy it already has


@dataclass(frozen=True)
class RunStart:
    run_id: str
    task_id: str
    agent: str
    prompt: str
    workspace: str


class Workspaces(Protocol):
    """Isolated copies of a repository for edit runs."""

    async def create(self, workspace: str, label: str) -> WorkTree:
        """A fresh copy on a throwaway branch; raises `CodingAgentError` when *workspace* is not a git repository."""
        ...

    async def finish(self, tree: WorkTree) -> WorkChange | None:
        """Commit what the agent changed onto the branch; None, and the copy removed, when nothing changed."""
        ...

    async def diff(self, tree: WorkTree) -> str:
        """What the branch changed, as a unified diff against the commit it was made from."""
        ...


@dataclass(frozen=True)
class ShellResult:
    """A command north ran. `refused` means the user would not let it run."""

    exit_code: int | None
    output: str = ""
    refused: bool = False
    error: str = ""


class Shell(Protocol):
    """Runs a command the way north runs any: ruled on by the approval layer, under the OS sandbox."""

    async def run(self, command: str, workspace: str, *, task_id: str, timeout: int) -> ShellResult: ...


class Verifier(Protocol):
    async def verify(self, tree: WorkTree, task_id: str) -> Verification:
        """Run the project's own tests on the agent's changes, in its copy."""
        ...


class Lander(Protocol):
    async def land(
        self, change: WorkChange, verification: Verification, task_id: str, review: Review | None = None
    ) -> Landing:
        """Offer the change to the user and, when they agree, apply it; always clean up the copy."""
        ...


class RunRecorder(Protocol):
    """Where a run is written down, so it can be seen on the dashboard and resumed."""

    async def live_run(self, task_id: str, agent: str, mode: str) -> LiveRun | None: ...

    async def start(self, run: RunStart) -> None: ...

    async def remember(self, run_id: str, state: Mapping[str, Any]) -> None: ...

    async def record(self, run_id: str, task_id: str, event: str, data: Mapping[str, Any]) -> None: ...

    async def finish(self, run_id: str, outcome: RunOutcome) -> None: ...
