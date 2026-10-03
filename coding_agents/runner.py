"""One delegated coding run, start to finish: pick an agent, record it, run it, record how it ended."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from coding_agents.base import CodingBackend, LiveRun, RunRecorder, RunStart, Workspaces
from coding_agents.exceptions import BackendUnavailableError, CodingAgentError
from coding_agents.gate import GateSessions
from coding_agents.models import (
    EventKind,
    FailureKind,
    GateAccess,
    Mode,
    RunEvent,
    RunOutcome,
    RunSpec,
    WorkChange,
    WorkTree,
)
from utils.ids import generate_id

logger = logging.getLogger(__name__)

AGENT_PREFIX = "coding:"


@dataclass(frozen=True)
class RunReport:
    run_id: str
    backend: str
    outcome: RunOutcome
    change: WorkChange | None = None  # what an edit run left on its branch


class CodingRunner:
    """Runs a task on an installed coding agent, and leaves the run on the record.

    An edit run also needs somewhere to work (`workspaces`), a way to tell the gate which run is asking
    (`sessions`) and where the gate is (`gate_url`); without all three only planning is offered.
    """

    def __init__(
        self,
        backends: Mapping[str, CodingBackend],
        recorder: RunRecorder,
        *,
        workspaces: Workspaces | None = None,
        sessions: GateSessions | None = None,
        gate_url: str = "",
    ) -> None:
        self._backends = dict(backends)
        self._recorder = recorder
        self._workspaces = workspaces
        self._sessions = sessions
        self._gate_url = gate_url

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._backends)

    @property
    def can_edit(self) -> bool:
        return bool(self._workspaces and self._sessions and self._gate_url)

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: str,
        guidance: str = "",
        backend: str | None = None,
        mode: Mode = Mode.PLAN,
    ) -> RunReport:
        """Run *task* in *workspace*; a re-planned task that finds its own live run continues that session."""
        if mode is Mode.EDIT and not self.can_edit:
            raise CodingAgentError("edit runs are not set up here")
        chosen, version = await self._choose(backend)
        agent = AGENT_PREFIX + chosen.name
        live = await self._live(task_id, agent, mode)
        run_id = live.run_id if live else generate_id()
        tree = await self._tree(live, workspace, run_id) if mode is Mode.EDIT else None
        spec = RunSpec(
            task=task,
            workspace=tree.path if tree else workspace,
            session_id=live.session_id if live else str(uuid.uuid4()),
            mode=mode,
            resume=live is not None,
            guidance=guidance,
        )
        # Written before the process starts, so a crash can still find the session and the copy.
        await self._recorder.start(RunStart(run_id, task_id, agent, task, workspace))
        await self._recorder.remember(run_id, _state(chosen, spec, version, tree))
        outcome, change = await self._execute(chosen, spec, run_id, task_id, tree)
        await self._recorder.finish(run_id, outcome)
        return RunReport(run_id, chosen.name, outcome, change)

    async def _choose(self, wanted: str | None) -> tuple[CodingBackend, str]:
        """The backend asked for, or the first that can take a run; why none can, when none can."""
        if wanted is not None and wanted not in self._backends:
            raise BackendUnavailableError(f"{wanted!r} is not a coding agent north can run ({', '.join(self.names)})")
        reasons = []
        for name in [wanted] if wanted else self.names:
            availability = await self._backends[name].probe()
            if availability.available:
                return self._backends[name], availability.version
            reasons.append(f"{name}: {availability.reason}")
        raise BackendUnavailableError("; ".join(reasons) or "no coding agent is installed")

    async def _live(self, task_id: str, agent: str, mode: Mode) -> LiveRun | None:
        """An earlier run of this task to continue. An edit run is only continued in the copy it still has."""
        live = await self._recorder.live_run(task_id, agent, mode.value)
        if live is not None and mode is Mode.EDIT and not (live.worktree and Path(live.worktree.path).is_dir()):
            return None
        return live

    async def _tree(self, live: LiveRun | None, workspace: str, run_id: str) -> WorkTree:
        assert self._workspaces is not None
        return live.worktree if live else await self._workspaces.create(workspace, f"coding-{run_id[:8]}")

    async def _execute(
        self, backend: CodingBackend, spec: RunSpec, run_id: str, task_id: str, tree: WorkTree | None
    ) -> tuple[RunOutcome, WorkChange | None]:
        async def sink(event: RunEvent) -> None:
            try:
                if event.kind is EventKind.STARTED:
                    await self._recorder.remember(run_id, {"provider": backend.provider, **event.data})
                await self._recorder.record(run_id, task_id, event.kind.value, event.data)
            except Exception:
                logger.warning("could not record %s for coding run %s", event.kind, run_id, exc_info=True)

        session = self._sessions.issue(run_id, task_id, tree.path) if tree and self._sessions else None
        if session:
            spec = _with_gate(spec, GateAccess(self._gate_url, session.token))
        try:
            outcome = await backend.run(spec, sink)
        except asyncio.CancelledError:
            cancelled = RunOutcome(False, "", spec.session_id, failure=FailureKind.CANCELLED, error="Run cancelled.")
            await asyncio.shield(self._finish_tree(run_id, task_id, tree))
            await asyncio.shield(self._recorder.finish(run_id, cancelled))
            raise
        except Exception as exc:
            logger.exception("coding run %s failed unexpectedly", run_id)
            outcome = RunOutcome(
                False, "", spec.session_id, failure=FailureKind.ERROR, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            if session and self._sessions:
                self._sessions.revoke(session.token)
        return outcome, await self._finish_tree(run_id, task_id, tree)

    async def _finish_tree(self, run_id: str, task_id: str, tree: WorkTree | None) -> WorkChange | None:
        """Commit what the agent changed, even when the run failed: partial work is still worth seeing."""
        if tree is None or self._workspaces is None:
            return None
        try:
            change = await self._workspaces.finish(tree)
        except Exception:
            logger.exception("could not commit the changes of coding run %s; the copy is left as it is", run_id)
            return None
        if change is not None:
            await self._recorder.record(
                run_id,
                task_id,
                "changes",
                {
                    "branch": tree.branch,
                    "files": len(change.files),
                    "insertions": change.insertions,
                    "deletions": change.deletions,
                },
            )
        return change


def _state(backend: CodingBackend, spec: RunSpec, version: str, tree: WorkTree | None) -> dict[str, object]:
    state: dict[str, object] = {
        "provider": backend.provider,
        "session_id": spec.session_id,
        "cli_version": version,
        "workspace": spec.workspace,
        "mode": spec.mode.value,
        "resumed": spec.resume,
    }
    if tree:
        state |= {"worktree": tree.path, "branch": tree.branch, "base_sha": tree.base_sha}
    return state


def _with_gate(spec: RunSpec, gate: GateAccess) -> RunSpec:
    return replace(spec, gate=gate)
