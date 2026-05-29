"""Extraction pipeline: reads new Ledger entries and appends insights to context documents.

See README Section 5.4.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path

from context.base import ContextStore
from context.models import ContextDocument
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from utils.ids import generate_id
from utils.time import utcnow

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 120
_BATCH_SIZE = 50
_MAX_CONCURRENT_EXTRACTIONS = 5  # semaphore cap: fast enough, stays under rate limits
_WATERMARK_FILENAME = "extraction_watermark.txt"

_DOCUMENT_MAP: dict[str, ContextDocument] = {
    "public": ContextDocument.PUBLIC,
    "judgement_rules": ContextDocument.JUDGEMENT_RULES,
    "north_stars": ContextDocument.NORTH_STARS,
}

_SKIPPED_SOURCES = {LedgerSource.SYSTEM, LedgerSource.INFERENCE_ROUTER}
# Failed-task entries carry noise (error messages, stack traces) rather than
# durable facts about the user.  Sending them to the LLM wastes budget.
_SKIPPED_STATUSES = {LedgerStatus.FAILED}

_EXTRACTION_PROMPT = """\
You are the extraction pipeline for a personal AI operating system.

Below is a new event from the system's audit log:

Source: {source}
Action: {action}
Input: {input}
Output: {output}

Your job: decide if this event reveals something NEW, MEANINGFUL, and DURABLE about \
the user — a preference, habit, goal, constraint, or decision pattern worth remembering.

If yes, respond with JSON in exactly this format:
{{"extract": true, "document": "<public|judgement_rules|north_stars>", \
"delta": "<one concise line>"}}

If no, respond with:
{{"extract": false}}

Rules:
- "public" for general non-sensitive facts (schedule patterns, preferences, career details).
- "judgement_rules" for how the user makes decisions (approvals, rejections, thresholds).
- "north_stars" for goals across time horizons.
- Never extract internal system events, error messages, or transient state.
- Be conservative: when in doubt, respond with {{"extract": false}}.
"""


class ExtractionPipeline:
    """Background job: reads new Ledger entries and extracts meaningful context deltas.

    Tracks a timestamp watermark persisted to `north_home/extraction_watermark.txt`.
    Runs on `poll_interval_seconds` cadence. Qualifying entries are sent to the
    high_volume pool; successful extractions are appended to the appropriate context
    document and logged back to the Ledger with source=system.
    """

    def __init__(
        self,
        ledger: LedgerWriter,
        context_store: ContextStore,
        inference_router: InferenceRouter,
        north_home: Path,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._context_store = context_store
        self._inference_router = inference_router
        self._watermark_path = north_home / _WATERMARK_FILENAME
        self._poll_interval = poll_interval_seconds

    async def run(self) -> None:
        """Loop forever, polling for new entries. Returns only on cancellation."""
        while True:
            try:
                await self._process_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ExtractionPipeline: error in batch, continuing")
            await asyncio.sleep(self._poll_interval)

    async def run_once(
        self, since: datetime.datetime | None = None
    ) -> int:
        """Process one batch and return the count of extractions made."""
        return await self._process_batch(since_override=since)

    # ------------------------------------------------------------------ #

    def _filter_valid_entries(self, entries: list[LedgerEntry]) -> list[LedgerEntry]:
        """Skip internal/failed entries, saving watermarks for them, return valid ones."""
        valid: list[LedgerEntry] = []
        for entry in entries:
            if entry.source in _SKIPPED_SOURCES or entry.status in _SKIPPED_STATUSES:
                self._save_watermark(entry.timestamp)
            else:
                valid.append(entry)
        return valid

    async def _run_extractions_concurrently(
        self, entries: list[LedgerEntry]
    ) -> list[bool | Exception]:
        """Perform concurrent extraction checks with a semaphore rate-limit cap."""
        sem = asyncio.Semaphore(_MAX_CONCURRENT_EXTRACTIONS)
        results: list[bool | Exception] = [False] * len(entries)

        async def _bounded(idx: int, entry: LedgerEntry) -> None:
            async with sem:
                try:
                    results[idx] = await self._process_entry(entry)
                except Exception as exc:
                    results[idx] = exc

        await asyncio.gather(*[_bounded(i, e) for i, e in enumerate(entries)])
        return results

    def _process_extraction_results(
        self, entries: list[LedgerEntry], results: list[bool | Exception]
    ) -> int:
        """Process extraction output, saving watermarks sequentially until any error."""
        extractions = 0
        for entry, result in zip(entries, results, strict=True):
            if isinstance(result, Exception):
                logger.exception(
                    "ExtractionPipeline: failed on entry %s — watermark NOT advanced past this "
                    "point, will retry",
                    entry.id,
                )
                break
            if result:
                extractions += 1
            self._save_watermark(entry.timestamp)

        return extractions

    async def _process_batch(
        self, since_override: datetime.datetime | None = None
    ) -> int:
        since = since_override or self._load_watermark()
        entries = await self._ledger.query(
            LedgerFilters(since=since, limit=_BATCH_SIZE)
        )
        # query returns DESC; process oldest first so watermark advances correctly
        entries = list(reversed(entries))
        if not entries:
            return 0

        to_process = self._filter_valid_entries(entries)
        if not to_process:
            return 0

        results = await self._run_extractions_concurrently(to_process)
        return self._process_extraction_results(to_process, results)

    async def _process_entry(self, entry: LedgerEntry) -> bool:
        """Ask the LLM whether this entry yields a user fact worth storing."""
        prompt = _EXTRACTION_PROMPT.format(
            source=entry.source.value,
            action=entry.action or "",
            input=entry.input or "",
            output=entry.output or "",
        )

        response = await self._inference_router.complete(
            CompletionRequest(
                prompt=prompt,
                priority=PoolPriority.LOW,
                component="extraction_pipeline",
                task_id=entry.task_id,
                json_mode=True,
            )
        )

        try:
            result = json.loads(response.text.strip())
        except (json.JSONDecodeError, ValueError):
            return False

        if not result.get("extract"):
            return False

        doc_key = result.get("document", "public")
        delta = str(result.get("delta", "")).strip()
        if not delta or doc_key not in _DOCUMENT_MAP:
            return False

        doc = _DOCUMENT_MAP[doc_key]
        await self._context_store.append(doc, delta)

        await self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=entry.task_id,
            action=f"extraction: {doc.value} updated",
            output=delta,
            status=LedgerStatus.COMPLETED,
        ))
        return True

    def _load_watermark(self) -> datetime.datetime | None:
        if not self._watermark_path.exists():
            return None
        text = self._watermark_path.read_text(encoding="utf-8").strip()
        try:
            return datetime.datetime.fromisoformat(text)
        except ValueError:
            return None

    def _save_watermark(self, ts: datetime.datetime) -> None:
        # Advance by 1µs so the next batch's `>=` query excludes this entry.
        advanced = ts + datetime.timedelta(microseconds=1)
        self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then atomically rename so a crash mid-write
        # never leaves the watermark in a partially-written state.
        tmp = self._watermark_path.with_suffix(".tmp")
        tmp.write_text(advanced.isoformat(), encoding="utf-8")
        os.replace(tmp, self._watermark_path)
