"""Running a mutating agent in an isolated git worktree.

Concurrent mutating runs must not share a working tree, so the coder (and any
agent in ``WORKTREE_ISOLATION_AGENTS``) can be given a throwaway branch and its
changes applied back under the workspace lock. With ``best_of_n > 1`` several
attempts run in parallel and only the best one is integrated.

Split out of ``Orchestrator`` because none of it touches task lifecycle: it needs
the worktree settings, somewhere to report to, and a way to run one agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.workspace_lock import workspace_lock
from ledger import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.best_of_n import CandidateOutcome, any_viable, select_best
from orchestrator.constants import WORKTREE_ISOLATION_AGENTS
from orchestrator.worktree import GitWorktreeManager, IntegrationResult, Worktree, WorktreeError
from tools._path import handoff_dir_for
from utils.ids import generate_id

logger = logging.getLogger(__name__)

# Per-candidate test-command timeout for best-of-N selection.
_BEST_OF_N_TEST_TIMEOUT: int = 300


def _remove_handoff_dir(path: Path) -> None:
    """Delete a candidate's handoff directory. Blocking - call via to_thread."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _promote_handoff_dir(winner: Path, canonical: Path) -> None:
    """Move the winning candidate's handoff dir into the canonical task dir.

    Blocking (a whole directory tree) - call via to_thread. Falls back to
    copy+delete when the rename crosses a filesystem boundary.
    """
    if not winner.exists():
        return
    if canonical.exists():
        shutil.rmtree(canonical, ignore_errors=True)
    try:
        winner.rename(canonical)
    except OSError:
        shutil.copytree(winner, canonical, dirs_exist_ok=True)
        shutil.rmtree(winner, ignore_errors=True)


class AgentIsolation:
    """Decides whether an agent run is isolated, and runs it either way."""

    def __init__(
        self,
        *,
        enabled: bool,
        worktree_root: str,
        best_of_n: int,
        test_command: str,
        stream_manager: Any,
        write_ledger: Callable[[LedgerEntry], Awaitable[None]],
        run_agent: Callable[[Agent, AgentPayload], Awaitable[AgentResult]],
    ) -> None:
        self._enabled = enabled
        self._root = worktree_root
        self._candidates = max(1, best_of_n)
        self._test_command = test_command.strip()
        self._stream_manager = stream_manager
        self._write_ledger = write_ledger
        self._run_agent = run_agent

    def _should_isolate(self, agent: Agent, payload: AgentPayload) -> bool:
            """True when this agent's run should happen in an isolated git worktree."""
            return self._enabled and bool(payload.workspace) and agent.name in WORKTREE_ISOLATION_AGENTS

    async def run(self, agent: Agent, payload: AgentPayload) -> AgentResult:
            """Run *agent* in a dedicated git worktree when isolation applies, else directly.

            The agent works on a throwaway branch so concurrent mutating runs never
            share a working tree. On success its changes are applied back onto the base
            workspace under the shared workspace lock (so the apply itself is
            serialized); on failure or cancellation the worktree is discarded. Any
            setup problem - not a git repo, or a worktree that will not create - falls
            back to a normal in-place run, so isolation can never block a task.
            """
            if not self._should_isolate(agent, payload):
                return await self._run_agent(agent, payload)

            manager = GitWorktreeManager(
                payload.workspace,
                root=Path(self._root) if self._root else None,
            )
            if not await manager.is_git_repo():
                return await self._run_agent(agent, payload)

            if self._candidates > 1 and agent.name == "coder":
                return await self._run_best_of_n(agent, payload, manager)

            try:
                wt = await manager.create(f"{agent.name}-{payload.task_id}")
            except WorktreeError:
                logger.warning("worktree: create failed for agent %s; running in-place", agent.name, exc_info=True)
                return await self._run_agent(agent, payload)

            isolated_payload = payload.model_copy(update={"workspace": wt.path})
            try:
                result = await self._run_agent(agent, isolated_payload)
                integration = await manager.integrate(wt, lock=workspace_lock(payload.workspace))
                await self._emit_integration(payload.task_id, agent.name, integration)
                return result
            except BaseException:
                await self._discard_worktree(manager, wt)
                raise

    async def _run_best_of_n(self, agent: Agent, payload: AgentPayload, manager: GitWorktreeManager) -> AgentResult:
            """Run N isolated coder attempts in parallel; integrate only the best (#11).

            Each attempt runs in its own worktree. Candidates are scored deterministically
            (viable > tests-pass > smaller diff); the winner is applied back onto the base
            tree and the losers are discarded. Falls back to a single in-place run when no
            worktree can be created.
            """
            n = self._candidates
            pairs: list[tuple[Worktree, AgentPayload, str]] = []
            for i in range(n):
                cand_task_id = f"{payload.task_id}__bo{i}"
                try:
                    wt = await manager.create(f"{agent.name}-{payload.task_id}-bo{i}")
                except WorktreeError:
                    logger.warning("best-of-n: worktree %d create failed; skipping", i, exc_info=True)
                    continue
                pairs.append(
                    (
                        wt,
                        payload.model_copy(
                            update={
                                "workspace": wt.path,
                                "task_id": cand_task_id,
                                "run_id": generate_id(),
                                "attempt": i,
                            }
                        ),
                        cand_task_id,
                    )
                )

            if not pairs:
                return await self._run_agent(agent, payload)

            integrated_wt: Worktree | None = None
            try:
                results = await asyncio.gather(
                    *[self._run_agent(agent, p) for _, p, _ in pairs],
                    return_exceptions=True,
                )

                outcomes: list[CandidateOutcome] = []
                for i, ((wt, _, _), res) in enumerate(zip(pairs, results, strict=False)):
                    succeeded = not isinstance(res, BaseException)
                    diff_lines = await manager.diff_line_count(wt)
                    tests_passed = await self._candidate_tests_pass(wt) if succeeded and diff_lines else None
                    outcomes.append(
                        CandidateOutcome(
                            index=i,
                            succeeded=succeeded,
                            changed=diff_lines > 0,
                            tests_passed=tests_passed,
                            diff_lines=diff_lines,
                        )
                    )

                winner = select_best(outcomes)
                await self._stream_manager.emit(
                    payload.task_id,
                    "best_of_n",
                    {"candidates": len(outcomes), "winner": winner, "viable": any_viable(outcomes)},
                )

                winner_result: AgentResult | None = None
                for i, (wt, _, cand_task_id) in enumerate(pairs):
                    if i == winner and not isinstance(results[i], BaseException):
                        integration = await manager.integrate(wt, lock=workspace_lock(payload.workspace))
                        integrated_wt = wt
                        await self._emit_integration(payload.task_id, agent.name, integration)
                        winner_result = results[i]  # type: ignore[assignment]
                        # Promote winning candidate handoff directory to canonical task directory
                        await asyncio.to_thread(
                            _promote_handoff_dir,
                            Path(handoff_dir_for(cand_task_id)),
                            Path(handoff_dir_for(payload.task_id)),
                        )
                    else:
                        await self._discard_worktree(manager, wt)
                        await asyncio.to_thread(_remove_handoff_dir, Path(handoff_dir_for(cand_task_id)))

                if winner_result is not None:
                    return winner_result
                # No viable winner: surface the first real result, or re-raise the first error.
                first = results[0]
                if isinstance(first, BaseException):
                    raise first
                return first  # type: ignore[return-value]
            finally:
                # Shielded: an await in a `finally` re-raises immediately once the task
                # is being cancelled, which would abandon every candidate after the
                # first - leaving git worktrees and scratch directories behind on every
                # cancelled best-of-N run. Same guard the task-registry cleanup uses.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(self._cleanup_candidates(manager, pairs, integrated_wt, payload.task_id))

    async def _cleanup_candidates(
            self,
            manager: GitWorktreeManager,
            pairs: list[tuple[Worktree, AgentPayload, str]],
            integrated: Worktree | None,
            task_id: str,
        ) -> None:
            """Discard every losing worktree and scratch directory. Best-effort."""
            canonical = Path(handoff_dir_for(task_id))
            for wt, _, cand_task_id in pairs:
                if wt is not integrated:
                    await self._discard_worktree(manager, wt)
                cand_handoff = Path(handoff_dir_for(cand_task_id))
                if cand_handoff != canonical:
                    await asyncio.to_thread(_remove_handoff_dir, cand_handoff)

    async def _candidate_tests_pass(self, wt: Worktree) -> bool | None:
            """Run the configured best-of-N test command in *wt*; True/False, or None if unset."""
            cmd = self._test_command
            if not cmd:
                return None
            proc = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=wt.path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                code = await asyncio.wait_for(proc.wait(), timeout=_BEST_OF_N_TEST_TIMEOUT)
                return code == 0
            except (TimeoutError, OSError):
                if proc is not None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
                return False
            except BaseException:
                if proc is not None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
                raise

    async def _discard_worktree(self, manager: GitWorktreeManager, wt: Worktree) -> None:
            """Best-effort cleanup of an isolated worktree after a failed/cancelled run."""
            try:
                await manager.remove(wt, keep_branch=False)
            except Exception:
                logger.warning("worktree: failed to discard %s", wt.path, exc_info=True)

    async def _emit_integration(self, task_id: str, agent: str, integration: IntegrationResult) -> None:
            """Surface a worktree integration outcome to the UI and, on conflict, the ledger."""
            await self._stream_manager.emit(
                task_id,
                "worktree_integrated",
                {
                    "agent": agent,
                    "applied": integration.applied,
                    "changed": integration.changed,
                    "conflicted": integration.conflicted,
                    "branch": integration.branch,
                },
            )
            if integration.conflicted:
                logger.warning(
                    "worktree: %s changes conflicted on integrate - retained branch %s for manual merge",
                    agent,
                    integration.branch,
                )
                await self._write_ledger(
                    LedgerEntry.new(
                        source=LedgerSource.SYSTEM,
                        task_id=task_id,
                        agent=agent,
                        action="worktree_conflict",
                        output=(
                            f"{agent}'s isolated changes could not be applied cleanly (they overlap other "
                            f"changes). They are preserved on branch '{integration.branch}' for manual merge."
                        ),
                        status=LedgerStatus.COMPLETED,
                    )
                )
