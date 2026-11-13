"""One delegated coding run, start to finish: pick an agent, record it, run it, record how it ended."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from coding_agents.base import CodingBackend, Lander, LiveRun, RunRecorder, RunStart, Verifier, Workspaces
from coding_agents.constants import DEFAULT_MAX_TURNS, EDIT_MAX_TURNS, SAME_AGENT_REVIEW_MODELS
from coding_agents.exceptions import BackendUnavailableError, CodingAgentError
from coding_agents.gate import GateSessions
from coding_agents.models import (
    PAUSING_FAILURES,
    AskAccess,
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
from coding_agents.review import landed_review_task, parse_review, review_guidance, review_task
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
    earlier_branches: tuple[str, ...] = ()  # branches earlier edit rounds of this task left, still there


@dataclass(frozen=True)
class _Reviewer:
    """The agent that reads a change, and the model it reads with (None: its own default)."""

    name: str
    model: str | None = None
    same_agent: bool = False  # the author's own agent, in a new session

    @property
    def label(self) -> str:
        """How the review names its source, so a same-agent review is never taken for another agent's."""
        if not self.same_agent:
            return self.name
        return f"{self.name} ({self.model or 'its default model'}, a separate session from the author's)"


def _other_model(agent: str, authored: str | None) -> str | None:
    """The first model listed for *agent* that is not the one it wrote with; None when none is listed."""
    listed = SAME_AGENT_REVIEW_MODELS.get(agent, ())
    used = (authored or "").split("[")[0].strip().lower()  # Claude reports "claude-opus-5-5[1m]"
    others = [model for model in listed if not used.startswith(model)]
    return others[0] if others else None


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
        gate_url: str | Callable[[], str] = "",
        ask_url: str | Callable[[], str] = "",
        verifier: Verifier | None = None,
        lander: Lander | None = None,
    ) -> None:
        self._backends = dict(backends)
        self._recorder = recorder
        self._workspaces = workspaces
        self._sessions = sessions
        self._gate_url = gate_url
        self._ask_url = ask_url
        self._verifier = verifier
        self._lander = lander
        # The model each agent last wrote a task's change with, as the agent reported it: a review by the
        # same agent uses another model. Kept in memory: after a restart the review takes the first listed.
        self._authored: dict[tuple[str, str], str] = {}
        # The branches each task's edit rounds left behind, oldest first: a task that edits three times
        # left three branches in the user's repository and said so only one round at a time.

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
        asking: bool = True,
        model: str | None = None,
    ) -> RunReport:
        """Run *task* in *workspace*; a re-planned task that finds its own live run continues that session.

        *review* has another coding agent read an edit's diff before it is offered (see `_reviewer`).
        *fresh* never resumes: a review must not continue some earlier plan run of the same agent.
        *asking* lets the agent ask north a question (`ask_north`), when north is set up to answer.
        *model* overrides the agent's own default model.
        """
        if mode is Mode.EDIT and not self.can_edit:
            raise CodingAgentError("edit runs are not set up here")
        chosen, version = await self._choose(backend)
        agent = AGENT_PREFIX + chosen.name
        live = None if fresh else await self._live(task_id, agent, mode)
        run_id = live.run_id if live else generate_id()
        tree = await self._tree(live, workspace, run_id) if mode is Mode.EDIT else None
        spec = RunSpec(
            task=_in_chosen_folder(task, tree, workspace),
            workspace=tree.path if tree else workspace,  # the copy's top: its git and its sandbox rules start there
            session_id=live.session_id if live else str(uuid.uuid4()),
            mode=mode,
            resume=live is not None,
            guidance=guidance,
            max_turns=EDIT_MAX_TURNS if mode is Mode.EDIT else DEFAULT_MAX_TURNS,
            model=model,
        )
        # Written before the process starts, so a crash can still find the session and the copy.
        await self._recorder.start(RunStart(run_id, task_id, agent, task, workspace))
        await self._recorder.remember(run_id, _state(chosen, spec, version, tree))
        outcome, change, problem = await self._execute(chosen, spec, run_id, task_id, tree, asking)
        verification = landing = reviewed = None
        if change is not None and outcome.ok:
            verification, reviewed, landing = await self._land(
                run_id, task_id, change, spec.session_id, task, chosen.name if review else None
            )
        await self._recorder.finish(run_id, outcome)
        earlier = await self._track_branches(task_id, tree) if mode is Mode.EDIT else ()
        return RunReport(run_id, chosen.name, outcome, change, mode, verification, landing, reviewed, problem, earlier)

    async def _track_branches(self, task_id: str, tree: WorkTree | None) -> tuple[str, ...]:
        """The branches earlier rounds of *task_id* left that are still there, after this round (*tree*).

        Read from the run record, so a restart between rounds forgets none: kept in the runner's memory,
        they were neither named nor deleted after north restarted. An earlier branch whose changes are now
        in the working tree, because this round's same change was applied, is deleted: nothing on it is
        lost. One already gone (applied, or with nothing on it) is not named. Any other is kept for the
        user and named, so the answer lists every branch the task left rather than only the last.
        """
        if not task_id or self._workspaces is None:
            return ()
        remaining = []
        for earlier in await self._recorder.branches(task_id):
            if tree is not None and earlier.branch == tree.branch:
                continue
            try:
                gone = await self._workspaces.drop_if_in_place(earlier)
            except Exception:
                logger.warning("could not check whether branch %s is still needed", earlier.branch, exc_info=True)
                gone = False
            if not gone:
                remaining.append(earlier.branch)
        return tuple(remaining)

    async def unavailable(self, wanted: str | None = None) -> str:
        """Why no coding agent, or not *wanted*, can take a run now; "" when one can."""
        try:
            await self._choose(wanted)
        except BackendUnavailableError as exc:
            return str(exc)
        return ""

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
        self, backend: CodingBackend, spec: RunSpec, run_id: str, task_id: str, tree: WorkTree | None, asking: bool
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
                if event.kind is EventKind.INIT and spec.mode is Mode.EDIT and event.data.get("model"):
                    self._authored[task_id, backend.name] = str(event.data["model"])
                await self._recorder.record(run_id, task_id, event.kind.value, event.data)
            except Exception:
                logger.warning("could not record %s for coding run %s", event.kind, run_id, exc_info=True)

        session = None
        if self._sessions is not None and (tree or (asking and self._ask_url)):
            session = self._sessions.issue(run_id, task_id, tree.path if tree else spec.workspace, editing=bool(tree))
        if session and tree:
            spec = _with_gate(spec, GateAccess(_resolve(self._gate_url), session.token))
        if session and asking and self._ask_url:
            spec = replace(spec, ask=AskAccess(_resolve(self._ask_url), session.token))
        try:
            outcome = await backend.run(spec, sink)
        except asyncio.CancelledError:
            # A pause and a cancel arrive the same way. Recorded as cancelled, a paused run started over.
            paused = await asyncio.shield(self._recorder.task_is_paused(task_id))
            kind, why = (FailureKind.PAUSED, "Run paused.") if paused else (FailureKind.CANCELLED, "Run cancelled.")
            stopped = RunOutcome(False, "", spec.session_id, failure=kind, error=why)
            await asyncio.shield(self._finish_tree(run_id, task_id, tree, keep_empty=paused))
            await asyncio.shield(self._recorder.finish(run_id, stopped))
            raise
        except Exception as exc:
            logger.exception("coding run %s failed unexpectedly", run_id)
            outcome = RunOutcome(
                False, "", spec.session_id, failure=FailureKind.ERROR, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            if session and self._sessions:
                self._sessions.revoke(session.token)
        change, problem = await self._finish_tree(run_id, task_id, tree, keep_empty=outcome.failure in PAUSING_FAILURES)
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
        """Another read of *change* (see `_reviewer`), or None when nobody can review it or the diff cannot be had."""
        reviewer = await self._reviewer(author, task_id)
        if reviewer is None or self._workspaces is None:
            return None
        try:
            diff = await self._workspaces.diff(change.tree)
        except CodingAgentError:
            return None
        return await self._ask_reviewer(reviewer, review_task(task, diff), change.tree.path, task_id)

    async def review_landed(self, *, task_id: str, task: str, workspace: str, author: str) -> Review | None:
        """Another read of a task's finished change in *workspace* (see `_reviewer`).

        A review that cannot happen says why: skipped in silence, the report read as if the change had been
        reviewed (benchmark, 2026-11-11, Codex out of credits).
        """
        reviewer = await self._reviewer(author, task_id)
        if reviewer is None:
            return await self._why_no_reviewer(author)
        return await self._ask_reviewer(reviewer, landed_review_task(task), workspace, task_id)

    async def _reviewer(self, author: str, task_id: str) -> _Reviewer | None:
        """Who reads *author*'s change: another agent first, else the author's own agent in a new session.

        With Codex out of credits, Claude's changes went unreviewed while Claude itself could still read
        them. The same agent reviews on another model where one is listed, so it brings a second view.
        """
        for name, backend in self._backends.items():
            if name != author and (await backend.probe()).available:
                return _Reviewer(name)
        own = self._backends.get(author)
        if own is None or not (await own.probe()).available:
            return None
        return _Reviewer(author, _other_model(author, self._authored.get((task_id, author))), same_agent=True)

    async def _why_no_reviewer(self, author: str) -> Review | None:
        """Why no agent, the author's own included, could take the review; None when none is installed."""
        names = sorted(self._backends, key=lambda name: name == author)  # the other agents first
        if not names:
            return None
        reasons = [f"{name}: {(await self._backends[name].probe()).reason or 'it is not available'}" for name in names]
        return Review(names[0], ReviewVerdict.UNCLEAR, f"no coding agent could take the review ({'; '.join(reasons)})")

    async def _ask_reviewer(self, reviewer: _Reviewer, task: str, workspace: str, task_id: str) -> Review | None:
        try:
            report = await self.run(
                task_id=task_id,
                task=task,
                workspace=workspace,
                guidance=review_guidance(),
                backend=reviewer.name,
                mode=Mode.PLAN,
                fresh=True,
                asking=False,  # a reviewer reads and reports; it is not the user's to ask
                model=reviewer.model,
            )
        except CodingAgentError:
            return None
        if not report.outcome.ok:
            error = f"the review could not be completed: {report.outcome.error}"
            return Review(reviewer.label, ReviewVerdict.UNCLEAR, error)
        return parse_review(reviewer.label, report.outcome.text)

    async def _finish_tree(
        self, run_id: str, task_id: str, tree: WorkTree | None, *, keep_empty: bool = False
    ) -> tuple[WorkChange | None, str]:
        """Commit what the agent changed, even when the run failed: partial work is still worth seeing.

        Returns the change, and why it could not be saved when it could not: "no changes" and "changes that
        could not be saved" are different things to tell the user.
        """
        if tree is None or self._workspaces is None:
            return None, ""
        try:
            change = await self._workspaces.finish(tree, keep_empty=keep_empty)
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
                    # What the branch holds, on the record: the agent's own account of its edit can differ.
                    **({"diff": change.diff_shown()} if change.diff else {}),
                },
            )
        return change, ""


def _in_chosen_folder(task: str, tree: WorkTree | None, workspace: str) -> str:
    """*task*, saying where to work when the user chose a subfolder of the repository."""
    if tree is None or tree.start_in(workspace) == tree.path:
        return task
    folder = Path(tree.start_in(workspace)).relative_to(tree.path)
    return f"{task}\n\nThe user chose the folder `{folder}` of this repository. Work there."


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


def _resolve(url: str | Callable[[], str]) -> str:
    """A URL given as text, or looked up now; the server's port is known only once it listens."""
    return url() if callable(url) else url


def _with_gate(spec: RunSpec, gate: GateAccess) -> RunSpec:
    return replace(spec, gate=gate)
