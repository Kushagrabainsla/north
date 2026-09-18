"""Episode consolidator: builds episodic memory from the ledger.

Single writer for episodic.db. Runs as a background job (and once at startup),
reads the ledger for tasks that reached a terminal state since its watermark,
and records one episode per task with its outcome (success / failed / cancelled).
Because episodes are a projection of the ledger, the store can be rebuilt by
replaying from an earlier watermark.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource
from utils.prompts import load_prompt
from utils.repository import repository_identity

if TYPE_CHECKING:
    from memory.episodic import EpisodicStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 120
_BATCH_SIZE = 200
_PER_TASK_ROW_LIMIT = 500
_WATERMARK_FILENAME = "episode_watermark.txt"

# Terminal task-level actions written by the Orchestrator, mapped to an outcome.
_TERMINAL_OUTCOME: dict[str, str] = {
    "task_completed": "success",
    # Not a success to learn from. A run that finished with failures was being
    # recorded as an exemplar, and the skill distiller learns from exemplars -
    # so a task that told the user "I could not complete the summary" was
    # distilled into a permanent skill whose first step was "read the handoff
    # file; if it is missing, stop". Later runs followed it and failed the same
    # way, which is a fixed prompt being overruled by a learned procedure.
    "task_completed_with_failures": "partial",
    "task_failed": "failed",
    "task_cancelled": "cancelled",
}
_APPROVAL_ACTION_PREFIX = "approval_responded:"

# Ledger sources whose `input` is the user's original task prompt.
_PROMPT_SOURCES = frozenset(
    {LedgerSource.PROMPT, LedgerSource.MIC, LedgerSource.MANUAL_INJECTION, LedgerSource.WEBHOOK}
)


class EpisodeConsolidator:
    """Projects terminal ledger entries into episodic memory, one episode per task."""

    def __init__(
        self,
        ledger: LedgerWriter,
        episodic_store: EpisodicStore,
        inference_router: InferenceRouter,
        north_home: Path,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._episodic_store = episodic_store
        self._inference_router = inference_router
        self._watermark_path = north_home / _WATERMARK_FILENAME
        self._poll_interval = poll_interval_seconds
        # Prevents the background loop and a manual run_once from overlapping.
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        """Loop forever, consolidating on each tick. Returns only on cancellation."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EpisodeConsolidator: batch error, continuing")
            await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> int:
        """Process one batch and return the number of episodes recorded."""
        async with self._lock:
            return await self._process_batch()

    async def _process_batch(self) -> int:
        since = self._load_watermark()
        # Query oldest-first from the watermark so the watermark advances without skipping backlog entries.
        entries = await self._ledger.query(LedgerFilters(since=since, limit=_BATCH_SIZE, order_asc=True))
        recorded = 0
        for entry in entries:
            if entry.source == LedgerSource.SYSTEM and entry.action in _TERMINAL_OUTCOME and entry.task_id:
                try:
                    if await self._consolidate_task(entry.task_id, _TERMINAL_OUTCOME[entry.action]):
                        recorded += 1
                except Exception:
                    logger.exception(
                        "EpisodeConsolidator: failed on task %s - watermark not advanced, will retry",
                        entry.task_id,
                    )
                    break
            elif (
                entry.source == LedgerSource.APPROVAL
                and entry.action
                and entry.action.startswith(_APPROVAL_ACTION_PREFIX)
                and entry.task_id
                and self._is_learnable_approval(entry)
            ):
                # Prepared work can outlive its task. If its decision arrives
                # after the terminal episode was projected, rebuild that same
                # episode so the feedback remains part of task history.
                rows = await self._ledger.query(LedgerFilters(task_id=entry.task_id, limit=_PER_TASK_ROW_LIMIT))
                outcome = self._extract_terminal_outcome(rows)
                if outcome:
                    try:
                        if await self._consolidate_task(entry.task_id, outcome):
                            recorded += 1
                    except Exception:
                        logger.exception(
                            "EpisodeConsolidator: failed to add approval feedback for task %s",
                            entry.task_id,
                        )
                        break
            self._save_watermark(entry.timestamp)
        return recorded

    async def _consolidate_task(self, task_id: str, outcome: str) -> bool:
        """Build and upsert one episode for a terminal task. Returns True if recorded."""
        rows = await self._ledger.query(LedgerFilters(task_id=task_id, limit=_PER_TASK_ROW_LIMIT))
        if not rows:
            return False
        prompt = self._extract_prompt(rows)
        if not prompt:
            return False
        domain = self._extract_domain(rows)
        result = self._extract_result(rows, outcome)
        summary = await self._summarize(prompt, result, outcome)
        workspace = self._extract_workspace(rows)
        identity = repository_identity(workspace) if workspace else None
        await self._episodic_store.record(
            task_id=task_id,
            domain=domain,
            summary=summary,
            outcome=outcome,
            project_id=identity.project_id if identity else "",
            workspace_id=identity.workspace_id if identity else "",
        )
        return True

    @staticmethod
    def _extract_prompt(rows: list[LedgerEntry]) -> str:
        """The originating user prompt: the oldest prompt-sourced entry's input."""
        for entry in reversed(rows):  # rows are newest-first; want the oldest
            text = (entry.input or "").strip()
            if entry.source in _PROMPT_SOURCES and text:
                return text
        return ""

    @staticmethod
    def _extract_domain(rows: list[LedgerEntry]) -> str:
        """The task's domain, stamped on the classification entry by the Orchestrator."""
        for entry in rows:
            if entry.action and entry.action.startswith("classified_as_") and entry.agent_output:
                domain = entry.agent_output.get("domain")
                if isinstance(domain, str) and domain:
                    return domain
        return "general"

    @staticmethod
    def _extract_workspace(rows: list[LedgerEntry]) -> str:
        """Read the workspace stamped on task submission/resumption metadata."""
        for entry in rows:
            if entry.action in {"task_received", "task_resumed"} and entry.agent_output:
                workspace = entry.agent_output.get("workspace")
                if isinstance(workspace, str) and workspace:
                    return workspace
        return ""

    @staticmethod
    def _extract_result(rows: list[LedgerEntry], outcome: str) -> str:
        if outcome == "cancelled":
            return "Task was cancelled before completion."
        # Agent outputs in chronological order (rows are newest-first).
        outputs = [(e.output or "").strip() for e in reversed(rows) if e.source == LedgerSource.AGENT and e.output]
        if outcome == "failed":
            terminal = next(
                (
                    e.output
                    for e in rows
                    if e.source == LedgerSource.SYSTEM and e.action in _TERMINAL_OUTCOME and e.output
                ),
                "",
            )
            outputs.append(f"Failure: {terminal}".strip() if terminal else "The task failed.")
        feedback = EpisodeConsolidator._extract_approval_feedback(rows)
        if feedback:
            outputs.append("Approval feedback:\n" + "\n".join(feedback))
        combined = "\n".join(o for o in outputs if o).strip()
        return combined or "No output was produced."

    @staticmethod
    def _extract_terminal_outcome(rows: list[LedgerEntry]) -> str | None:
        for entry in rows:
            if entry.source == LedgerSource.SYSTEM and entry.action in _TERMINAL_OUTCOME:
                return _TERMINAL_OUTCOME[entry.action]
        return None

    @staticmethod
    def _is_learnable_approval(entry: LedgerEntry) -> bool:
        data = entry.agent_output or {}
        return bool(data.get("source")) and data.get("decision") in {"approved", "rejected"}

    @staticmethod
    def _extract_approval_feedback(rows: list[LedgerEntry]) -> list[str]:
        feedback: list[str] = []
        for entry in reversed(rows):
            if entry.source != LedgerSource.APPROVAL or not entry.action or not entry.action.startswith(
                _APPROVAL_ACTION_PREFIX
            ):
                continue
            if not EpisodeConsolidator._is_learnable_approval(entry):
                continue
            data = entry.agent_output or {}
            decision = str(data["decision"])
            source = str(data["source"])
            reason = str(data.get("reason") or "No reason provided.")
            edited = data.get("edited_fields") or []
            edit_note = f" Edited fields: {', '.join(str(field) for field in edited)}." if edited else ""
            feedback.append(f"Source {source}: user {decision} the proposal. Reason: {reason}.{edit_note}")
        return feedback

    async def _summarize(self, prompt: str, result: str, outcome: str) -> str:
        """LLM summary for retrieval, with a plain truncated fallback (tests/offline)."""
        fallback = f"Task: {prompt[:200]}\nResult: {result[:500]}"
        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=load_prompt("prompts/episode_summary.md").format(prompt=prompt, result=result[:3000]),
                    priority=PoolPriority.LOW,
                    component="episode_consolidator",
                    task_id=None,  # task already finished; no live cost to attribute
                )
            )
            text = response.text.strip()
            # Guard/classifier models sometimes return a bare float score; discard those.
            try:
                float(text)
                return fallback
            except ValueError:
                return text or fallback
        except Exception:
            logger.warning("EpisodeConsolidator: summarization failed, using fallback", exc_info=True)
            return fallback

    def _load_watermark(self) -> datetime.datetime | None:
        try:
            text = self._watermark_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        try:
            return datetime.datetime.fromisoformat(text) if text else None
        except ValueError:
            return None

    def _save_watermark(self, timestamp: datetime.datetime) -> None:
        try:
            advanced = timestamp + datetime.timedelta(microseconds=1)
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._watermark_path.with_suffix(".tmp")
            tmp.write_text(advanced.isoformat(), encoding="utf-8")
            os.replace(tmp, self._watermark_path)
        except OSError:
            logger.warning("EpisodeConsolidator: failed to persist watermark", exc_info=True)
