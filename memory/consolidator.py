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
from pathlib import Path
from typing import TYPE_CHECKING

from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource

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
    "task_completed_with_failures": "success",
    "task_failed": "failed",
    "task_cancelled": "cancelled",
}

# Ledger sources whose `input` is the user's original task prompt.
_PROMPT_SOURCES = frozenset(
    {LedgerSource.PROMPT, LedgerSource.MIC, LedgerSource.MANUAL_INJECTION, LedgerSource.WEBHOOK}
)

_SUMMARY_PROMPT = (
    "Summarize this completed AI task in 2-3 sentences for future retrieval. "
    "Include what was requested, what was done, and any key outcomes or decisions. "
    "If it failed, state plainly what went wrong so it is not repeated.\n\n"
    "Task: {prompt}\n\nResult: {result}"
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
        entries = await self._ledger.query(LedgerFilters(since=since, limit=_BATCH_SIZE))
        # query() returns newest-first; process oldest-first so the watermark advances safely.
        entries = list(reversed(entries))
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
        await self._episodic_store.record(task_id=task_id, domain=domain, summary=summary, outcome=outcome)
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
        combined = "\n".join(o for o in outputs if o).strip()
        return combined or "No output was produced."

    async def _summarize(self, prompt: str, result: str, outcome: str) -> str:
        """LLM summary for retrieval, with a plain truncated fallback (tests/offline)."""
        fallback = f"Task: {prompt[:200]}\nResult: {result[:500]}"
        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=_SUMMARY_PROMPT.format(prompt=prompt, result=result[:3000]),
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
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            self._watermark_path.write_text(timestamp.isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("EpisodeConsolidator: failed to persist watermark", exc_info=True)
