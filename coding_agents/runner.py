"""One delegated coding run, start to finish: pick an agent, record it, run it, record how it ended."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from coding_agents.base import CodingBackend, Lander, LiveRun, RunRecorder, RunStart, Verifier, Workspaces
from coding_agents.exceptions import BackendUnavailableError, CodingAgentError
from coding_agents.gate import GateSessions
from coding_agents.models import (
    EventKind,
    FailureKind,
    GateAccess,
    Landing,
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
from coding_agents.review import parse_review, review_guidance, review_task
from utils.ids import generate_id

logger = logging.getLogger(__name__)

AGENT_PREFIX = "coding:"


@dataclass(frozen=True)
class RunReport:
    run_id: str
    backend: str
    outcome: RunOutcome
    change: WorkChange | None = None  # what an edit run left on its branch
    mode: Mode = Mode.PLAN
    verification: Verification | None = None  # north's own run of the project's tests
    landing: Landing | None = None  # whether the change reached the working tree
    review: Review | None = None  # the other agent's read of the change
    problem: str = ""  # why the copy's changes could not be saved, when they could not


class CodingRunner:
    """Runs a task on an installed coding agent, and leaves the run on the record.

    An edit run also needs somewhere to work (`workspaces`), a way to tell the gate which run is asking
    (`sessions`) and where the gate is (`gate_url`); without all three only planning is offered. With a
    `verifier` and a `lander` as well, a finished edit is tested by north and then offered to the user.
    """

    def __init__(
        self,
        backends: Mapping[str, CodingBackend],
        recorder: RunRecorder,
        *,
        workspaces: Workspaces | None = None,
        sessions: GateSessions | None = None,
        gate_url: str = "",
        verifier: Verifier | None = None,
        lander: Lander | None = None,
    ) -> None:
        self._backends = dict(backends)
        self._recorder = recorder
        self._workspaces = workspaces
        self._sessions = sessions
        self._gate_url = gate_url
        self._verifier = verifier
        self._lander = lander

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
        review: bool = False,
        fresh: bool = False,
    ) -> RunReport:
        """Run *task* in *workspace*; a re-planned task that finds its own live run continues that session.

        *review* has the other coding agent, when there is one, read an edit's diff before it is offered.
        *fresh* never resumes: a review must not continue some earlier plan run of the same agent.
        """
        if mode is Mode.EDIT and not self.can_edit:
            raise CodingAgentError("edit runs are not set up here")
        chosen, version = await self._choose(backend)
        agent = AGENT_PREFIX + chosen.name
        live = None if fresh else await self._live(task_id, agent, mode)
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
        outcome, change, problem = await self._execute(chosen, spec, run_id, task_id, tree)
        verification = landing = reviewed = None
        if change is not None and outcome.ok:
            verification, reviewed, landing = await self._land(
                run_id, task_id, change, spec.session_id, task, chosen.name if review else None
            )
        await self._recorder.finish(run_id, outcome)
        return RunReport(run_id, chosen.name, outcome, change, mode, verification, landing, reviewed, problem)

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
            # Its copy is gone, so it can never resume; leaving it unfinished would show it paused for ever.
            gone = RunOutcome(False, "", live.session_id, failure=FailureKind.ERROR, error="its isolated copy is gone")
            await self._recorder.finish(live.run_id, gone)
            return None
        return live

    async def _tree(self, live: LiveRun | None, workspace: str, run_id: str) -> WorkTree:
        assert self._workspaces is not None
        if live and live.worktree:  # `_live` only returns an edit run that still has its copy
            return live.worktree
        return await self._workspaces.create(workspace, f"coding-{run_id[:8]}")

    async def _execute(
        self, backend: CodingBackend, spec: RunSpec, run_id: str, task_id: str, tree: WorkTree | None
    ) -> tuple[RunOutcome, WorkChange | None, str]:
        async def sink(event: RunEvent) -> None:
            try:
                if event.kind is EventKind.STARTED:
                    await self._recorder.remember(run_id, {"provider": backend.provider, **event.data})
                elif event.kind is EventKind.INIT and event.data.get("session_id"):
                    # An agent that picks its own session id (Codex's thread) says so as soon as it has one.
                    await self._recorder.remember(
                        run_id, {"provider": backend.provider, "session_id": event.data["session_id"]}
                    )
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
        change, problem = await self._finish_tree(run_id, task_id, tree)
        return outcome, change, problem

    async def _land(
        self, run_id: str, task_id: str, change: WorkChange, session_id: str, task: str, reviewing_for: str | None
    ) -> tuple[Verification | None, Review | None, Landing | None]:
        """Test the change ourselves, have the other agent read it, then offer it. A failure here never hides
        the agent's outcome: the copy and its branch are simply left as they are."""
        if self._verifier is None or self._lander is None:
            return None, None, None
        reviewed = None
        try:
            verification = await self._verifier.verify(change.tree, task_id)
            await self._recorder.record(
                run_id, task_id, "verification", {"state": verification.state.value, "command": verification.command}
            )
            # A change whose tests failed is not offered, so reading it would spend the user's quota for nothing.
            if reviewing_for is not None and verification.state is not VerificationState.FAILED:
                reviewed = await self._review(change, task, task_id, author=reviewing_for)
                if reviewed is not None:
                    await self._recorder.record(
                        run_id, task_id, "review", {"reviewer": reviewed.reviewer, "verdict": reviewed.verdict.value}
                    )
            landing = await self._lander.land(change, verification, task_id, reviewed)
            await self._recorder.record(run_id, task_id, "landing", {"state": landing.state.value})
        except asyncio.CancelledError:
            cancelled = RunOutcome(False, "", session_id, failure=FailureKind.CANCELLED, error="Run cancelled.")
            await asyncio.shield(self._recorder.finish(run_id, cancelled))
            raise
        except Exception:
            logger.exception("could not test or land the changes of coding run %s; the branch is left as it is", run_id)
            return None, None, None
        return verification, reviewed, landing

    async def _review(self, change: WorkChange, task: str, task_id: str, *, author: str) -> Review | None:
        """The other agent's read of *change*, or None when there is no other agent or the diff cannot be had."""
        other = await self._other_available(author)
        if other is None or self._workspaces is None:
            return None
        try:
            diff = await self._workspaces.diff(change.tree)
            report = await self.run(
                task_id=task_id,
                task=review_task(task, diff),
                workspace=change.tree.path,
                guidance=review_guidance(),
                backend=other,
                mode=Mode.PLAN,
                fresh=True,
            )
        except CodingAgentError:
            return None
        if not report.outcome.ok:
            return Review(other, ReviewVerdict.UNCLEAR, f"the review could not be completed: {report.outcome.error}")
        return parse_review(other, report.outcome.text)

    async def _other_available(self, author: str) -> str | None:
        """The first coding agent that is not *author* and can take a run now."""
        for name, backend in self._backends.items():
            if name != author and (await backend.probe()).available:
                return name
        return None

    async def _finish_tree(self, run_id: str, task_id: str, tree: WorkTree | None) -> tuple[WorkChange | None, str]:
        """Commit what the agent changed, even when the run failed: partial work is still worth seeing.

        Returns the change, and why it could not be saved when it could not: "no changes" and "changes that
        could not be saved" are different things to tell the user.
        """
        if tree is None or self._workspaces is None:
            return None, ""
        try:
            change = await self._workspaces.finish(tree)
        except Exception as exc:
            logger.exception("could not commit the changes of coding run %s; the copy is left as it is", run_id)
            problem = str(exc) if isinstance(exc, CodingAgentError) else f"could not save the changes: {exc}"
            await self._recorder.record(run_id, task_id, "copy_problem", {"problem": problem, "path": tree.path})
            return None, problem
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
        return change, ""


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
        state |= {"worktree": tree.path, "branch": tree.branch, "base_sha": tree.base_sha, "base": tree.base}
    return state


def _with_gate(spec: RunSpec, gate: GateAccess) -> RunSpec:
    return replace(spec, gate=gate)
