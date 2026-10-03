"""Writes a delegated coding run into the run store, so it shows on the dashboard and can resume."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from agents.models import AgentPayload, AgentResult
from coding_agents import AGENT_PREFIX, FailureKind, LiveRun, RunOutcome, RunStart, WorkTree
from orchestrator.agent_runs import AgentRunStore, RunStatus
from utils.execution_context import current_execution

# Runs that have not finished: the ones a re-planned task may pick back up.
_UNFINISHED = frozenset(status.value for status in RunStatus)
_SUMMARY_CHARS = 300
# Only these can still have a process; a paused or interrupted run has none, whatever a recycled pid says.
_MAY_BE_ALIVE = frozenset({RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL})
# Missing resources freeze work; they do not fail it (CODING_STYLE 13.5).
_FROZEN_BY = frozenset({FailureKind.RESOURCE, FailureKind.AUTH})


class AgentRunRecorder:
    """A coding run is a child row of the agent run that asked for it."""

    def __init__(self, store: AgentRunStore, *, pid_alive: Callable[[int], bool] | None = None) -> None:
        self._store = store
        self._pid_alive = pid_alive or _pid_alive
        self._started_at: dict[str, float] = {}

    async def live_run(self, task_id: str, agent: str, mode: str) -> LiveRun | None:
        """The newest unfinished *mode* run of *agent* for this task whose process is gone, if it left a session."""
        for run in reversed(await self._store.list_for_task(task_id)):
            if run.agent != agent or run.status not in _UNFINISHED or _latest(run.provider_state, "mode") != mode:
                continue
            pid = _latest(run.provider_state, "pid")
            if run.status in _MAY_BE_ALIVE and pid and self._pid_alive(int(pid)):
                continue  # still running right now: a second run, not a resume
            session_id = _latest(run.provider_state, "session_id")
            return LiveRun(run.run_id, str(session_id), _worktree(run.provider_state)) if session_id else None
        return None

    async def start(self, run: RunStart) -> None:
        parent = current_execution()
        payload = AgentPayload(
            task_id=run.task_id,
            run_id=run.run_id,
            parent_run_id=parent.run_id if parent else None,
            prompt=run.prompt,
            workspace=run.workspace,
            delegation_depth=1,
        )
        self._started_at[run.run_id] = time.monotonic()
        await self._store.start(payload, run.agent)

    async def remember(self, run_id: str, state: Mapping[str, Any]) -> None:
        await self._store.merge_provider_state(run_id, dict(state))

    async def record(self, run_id: str, task_id: str, event: str, data: Mapping[str, Any]) -> None:
        await self._store.record_event(run_id, task_id, event, dict(data))

    async def waiting(self, run_id: str, waiting: bool) -> None:
        """Show the run as waiting for an approval, or running again."""
        await self._store.set_status(run_id, RunStatus.WAITING_FOR_APPROVAL if waiting else RunStatus.RUNNING)

    async def reconcile(self) -> int:
        """After a restart, mark coding runs that say they are running but whose process is gone as interrupted.

        Without this a dead run reads as running on the dashboard for ever. Only a process that is really gone
        counts: one still alive is left alone, so a resume never starts a second agent beside it.
        """
        marked = 0
        for run in await self._store.list_unfinished(AGENT_PREFIX):
            if run.status not in (RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL):
                continue
            pid = _latest(run.provider_state, "pid")
            if pid and self._pid_alive(int(pid)):
                continue
            marked += await self._store.set_status(run.run_id, RunStatus.INTERRUPTED)
        return marked

    async def finish(self, run_id: str, outcome: RunOutcome) -> None:
        if outcome.failure in _FROZEN_BY:
            # A rate limit or a logged-out agent is not a failed task: keep the run, and the session, to resume.
            await self._store.pause(run_id, outcome.error)
            return
        if not outcome.ok:
            status = "cancelled" if outcome.failure is FailureKind.CANCELLED else "failed"
            await self._store.finish_with_error(run_id, status, outcome.error)
            return
        elapsed = time.monotonic() - self._started_at.pop(run_id, time.monotonic())
        await self._store.complete(
            run_id,
            AgentResult(
                output=outcome.text,
                summary=outcome.text[:_SUMMARY_CHARS],
                run_id=run_id,
                cost_usd=outcome.cost_usd,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                duration_ms=int(elapsed * 1000),
            ),
        )


def _latest(provider_state: Mapping[str, Any], key: str) -> Any:
    """The most recent value of *key* across a run's provider state (``{provider: [entries]}``)."""
    for entries in provider_state.values():
        for entry in reversed(entries):
            if entry.get(key):
                return entry[key]
    return None


def _worktree(provider_state: Mapping[str, Any]) -> WorkTree | None:
    path, branch, sha, base = (_latest(provider_state, key) for key in ("worktree", "branch", "base_sha", "base"))
    return WorkTree(str(path), str(branch), str(sha), str(base)) if path and branch and sha and base else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, owned by someone else
    return True
