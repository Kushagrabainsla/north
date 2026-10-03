"""What a coding backend is, and what north gives the runner to record a run.

The runner never imports north's run store or approval layer; the composition root hands it
implementations of the small protocols here (CODING_STYLE 6.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from coding_agents.models import Availability, RunEvent, RunOutcome, RunSpec

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


@dataclass(frozen=True)
class RunStart:
    run_id: str
    task_id: str
    agent: str
    prompt: str
    workspace: str


class RunRecorder(Protocol):
    """Where a run is written down, so it can be seen on the dashboard and resumed."""

    async def live_run(self, task_id: str, agent: str) -> LiveRun | None: ...

    async def start(self, run: RunStart) -> None: ...

    async def remember(self, run_id: str, state: Mapping[str, Any]) -> None: ...

    async def record(self, run_id: str, task_id: str, event: str, data: Mapping[str, Any]) -> None: ...

    async def finish(self, run_id: str, outcome: RunOutcome) -> None: ...
