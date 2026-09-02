"""The Definition-of-Done gate: does the recorded evidence say this is really done?

Two independent sources of truth, neither of which is the model's word for it:

* an **executable oracle** - the project's own test command, run by us, judged
  by its exit code (`orchestrator/verify_command.py`);
* the **recorded evidence** - which model produced each result and what the
  reviewer's structured verdict said, read back out of the ledger and scored by
  the pure evaluator in `orchestrator/dod.py`.

This module gathers both and records the verdict. It decides nothing about what
the task should do next; the orchestrator owns that.

Every path fails open. A gate that breaks a working task because its own harness
had a problem is worse than no gate, so an unrunnable command, a timeout, or any
unexpected error is "unknown" - never "failed".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import NamedTuple

from agents.constants import CODE_MUTATING_TOOLS
from ledger.base import LedgerFilters, LedgerWriter
from orchestrator.dod import DodResult, evaluate_engineering_dod
from orchestrator.journal import TaskJournal
from orchestrator.review import read_review_result
from orchestrator.verify_command import VERIFY_COMMAND_TIMEOUT, detect_verify_command

logger = logging.getLogger(__name__)

# How many recent ledger entries to scan when gathering a task's code evidence.
_LEDGER_SCAN_LIMIT: int = 200

# Exit codes meaning the command could not be run at all - 127 is "not found",
# 126 "not executable". A missing runner is unknown, not a test failure.
_UNRUNNABLE_EXIT_CODES: frozenset[int] = frozenset({126, 127})

_ENGINEERING_DOMAIN = "engineering"


class CodeEvidence(NamedTuple):
    """The recorded evidence a Definition-of-Done verdict is computed from."""

    coder_models: list[str]
    reviewer_models: list[str]
    change_applied: bool


class QualityGate:
    """Runs the verification oracle and evaluates the engineering Definition of Done."""

    def __init__(self, *, ledger: LedgerWriter, journal: TaskJournal, verify_command: str = "") -> None:
        self._ledger = ledger
        self._journal = journal
        self._verify_command = verify_command.strip()

    # ------------------------------------------------------------------ #
    #  The executable oracle                                               #
    # ------------------------------------------------------------------ #

    def resolve_command(self, workspace: str) -> str | None:
        """The verification command for a task: the explicit setting if configured,
        else a safe auto-detected one, else None (skip)."""
        return self._verify_command or detect_verify_command(workspace)

    async def run_verification(self, task_id: str, workspace: str) -> bool | None:
        """Run the independent verification oracle, recording what it found.

        Returns pass (True) / fail (False) / unknown (None), and records an
        ``auto_verify`` entry when a command was actually run.
        """
        try:
            command = self.resolve_command(workspace)
            if not command:
                return None
            await self._journal.emit(task_id, "auto_verify_started", {"command": command})
            passed = await self._run_command(workspace, command)
            await self._journal.record(
                task_id,
                "auto_verify",
                output=f"independent verification `{command}` {_outcome_word(passed)}",
                error_type=None if passed is not False else "auto_verify_failed",
                payload={"command": command, "passed": passed},
            )
            return passed
        except Exception:
            logger.debug("auto-verify failed for task %s", task_id, exc_info=True)
            return None

    async def _run_command(self, workspace: str, command: str) -> bool | None:
        """Run *command* in *workspace*; True/False by exit code, None when it could
        not be run. Output is discarded - only the exit code matters here."""
        if not workspace:
            return None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=workspace,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            return None
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=VERIFY_COMMAND_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return None
        if exit_code in _UNRUNNABLE_EXIT_CODES:
            return None
        return exit_code == 0

    # ------------------------------------------------------------------ #
    #  The recorded evidence                                               #
    # ------------------------------------------------------------------ #

    async def evaluate(
        self, task_id: str, domain: str, kind: str = "", auto_verify_passed: bool | None = None
    ) -> DodResult | None:
        """Evaluate the engineering Definition of Done and record the verdict.

        ``kind`` is the engineering_kind (e.g. bugfix/debug) so the gate can apply
        kind-specific fix-evidence checks. Returns the verdict, or None for
        non-engineering tasks and on any error.
        """
        if domain != _ENGINEERING_DOMAIN:
            return None
        try:
            evidence = await self._gather_evidence(task_id)
            result = evaluate_engineering_dod(
                change_applied=evidence.change_applied,
                coder_models=evidence.coder_models,
                reviewer_models=evidence.reviewer_models,
                review=read_review_result(task_id),
                kind=kind,
                auto_verify_passed=auto_verify_passed,
            )
            await self._record_verdict(task_id, result)
            return result
        except Exception:
            logger.debug("DoD evaluation failed for task %s", task_id, exc_info=True)
            return None

    async def _gather_evidence(self, task_id: str) -> CodeEvidence:
        """Read a task's code evidence from the ledger: which model the coder and
        reviewer used, and whether a code-mutating change was actually applied."""
        entries = await self._ledger.query_summaries(LedgerFilters(task_id=task_id, limit=_LEDGER_SCAN_LIMIT))
        coder_models: list[str] = []
        reviewer_models: list[str] = []
        change_applied = False
        for entry in entries:  # most-recent-first
            if entry.action != "agent_completed":
                continue
            models = [m.strip() for m in (entry.model_used or "").split(",") if m.strip()]
            if entry.agent == "coder":
                coder_models = coder_models or models
                if set(entry.tools_used or []) & CODE_MUTATING_TOOLS:
                    change_applied = True
            elif entry.agent == "reviewer":
                reviewer_models = reviewer_models or models
        return CodeEvidence(coder_models, reviewer_models, change_applied)

    async def _record_verdict(self, task_id: str, result: DodResult) -> None:
        """Record a DoD verdict to the ledger + stream (and log when unmet)."""
        await self._journal.record(
            task_id,
            "dod_evaluated",
            output="Definition of Done met" if result.passed else "; ".join(result.reasons),
            error_type=None if result.passed else "dod_unmet",
            payload={"passed": result.passed, "reasons": result.reasons},
        )
        if not result.passed:
            logger.warning("DoD not met for task %s: %s", task_id, "; ".join(result.reasons))

    async def report_unmet(self, task_id: str, reasons: list[str]) -> None:
        """Surface an unmet Definition of Done in the streamed answer (visible note)."""
        bullets = "; ".join(reasons)
        await self._journal.stream_note(
            task_id,
            f"\n\n> ⚠️ **Definition of Done not met:** {bullets}. Treat this as not fully done until confirmed.",
        )


def _outcome_word(passed: bool | None) -> str:
    if passed is None:
        return "could not run"
    return "passed" if passed else "failed"
