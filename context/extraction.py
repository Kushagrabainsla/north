"""Extraction pipeline: reads new Ledger entries and appends insights to context documents.

See README Section 5.4.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from context.base import ContextStore
from context.models import ContextDocument
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from utils.ids import generate_id
from utils.time import utcnow

if TYPE_CHECKING:
    from context.fact_store import FactStore

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

# Only the user's own messages are a valid source of facts about the user.
# Extracting from agent/system outputs let the pipeline learn the assistant's
# own (sometimes hallucinated) text as if it were user-stated fact — a
# self-reinforcing memory-poisoning loop. Facts come from what the USER wrote.
_USER_AUTHORED_SOURCES = frozenset(
    {LedgerSource.PROMPT, LedgerSource.MIC, LedgerSource.MANUAL_INJECTION}
)
# Failed-task entries carry noise (error messages, stack traces) rather than
# durable facts about the user.  Sending them to the LLM wastes budget.
_SKIPPED_STATUSES = {LedgerStatus.FAILED}

_EXTRACTION_PROMPT = """\
You are the memory extraction pipeline for a personal AI operating system.

Below is a message the USER wrote:

\"\"\"
{message}
\"\"\"

Extract a durable fact about the user ONLY IF the user states it explicitly in
this message. Durable means it will still be true or useful weeks from now.

Anti-fabrication contract — follow exactly:
- Extract ONLY information the user literally wrote above. Never infer, assume,
  generalize, or invent.
- Every name, company, person, number, or date in the fact MUST appear verbatim
  in the message above. If it is not in the message, you may not write it.
- Greetings, questions, commands, and small-talk reveal NO durable fact.
- This is the user's own message — it is NOT an assistant reply. Do not treat
  any AI-sounding content as a fact about the user.
- If you are not certain the user explicitly stated a durable fact, or the
  message contains none, return extract:false. When in doubt, return false.

If a durable fact is explicitly present, respond with JSON:
{{"extract": true, "document": "<public|judgement_rules|north_stars>", "delta": "<fact>"}}

Otherwise respond with:
{{"extract": false}}

Document rules:
- "public": stable identity facts the user stated — their name, role, employer,
  schedule, preferences, tools they use, people they work with.
- "judgement_rules": how the user decides — what they approve/reject, thresholds,
  priorities, communication style.
- "north_stars": goals with time horizons the user stated — career, projects,
  this week's focus.

Fact format:
- One sentence, third-person neutral, grounded only in the user's words.
- "public"/"judgement_rules": present tense ("User works at Y").
- "north_stars": goal-oriented phrasing ("User wants to X by [date/horizon]").
"""

_DEDUP_PROMPT = """\
You are checking whether a new memory fact is already captured in an existing document.

Existing document (last 2000 chars):
---
{existing}
---

New fact to add:
"{delta}"

Is the core information in the new fact ALREADY present in the document (even if worded differently)?
Reply with JSON only: {{"duplicate": true}} or {{"duplicate": false}}
"""

_MAX_DOCUMENT_CHARS = 8_000  # trim when a context doc exceeds this
_TRIM_TARGET_CHARS = 5_000  # target size after trimming
_BACKUP_INTERVAL_HOURS = 24  # minimum hours between full context backups

_TRIM_PROMPT = """\
The following personal context document ({doc_type}) has grown too long. Condense it by:
1. Merging duplicate or near-duplicate facts into one line.
2. Removing facts that are clearly outdated or no longer relevant — apply this aggressively for \
"north_stars" (goals with past deadlines), conservatively for "public" (stable identity facts) \
and "judgement_rules" (learned preferences that rarely expire).
3. Keeping every distinct fact that is still likely to be useful.

Return ONLY the condensed document text, no explanation.

Document:
---
{content}
---
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
        max_daily_cost_usd: float = 0.10,
        min_output_chars: int = 100,  # retained for config compatibility; unused
        min_input_chars: int = 12,
        max_concurrent: int = _MAX_CONCURRENT_EXTRACTIONS,
        fact_store: FactStore | None = None,
    ) -> None:
        self._ledger = ledger
        self._context_store = context_store
        self._inference_router = inference_router
        self._watermark_path = north_home / _WATERMARK_FILENAME
        self._archive_dir = north_home / "context_archive"
        self._backup_dir = north_home / "context_backup"
        self._poll_interval = poll_interval_seconds
        self._max_daily_cost = max_daily_cost_usd
        self._min_input_chars = min_input_chars
        self._max_concurrent = max_concurrent
        self._fact_store = fact_store
        # Prevents concurrent _process_batch calls (background loop + per-task
        # trigger) from reading the same watermark and double-processing entries.
        self._lock = asyncio.Lock()

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

    async def run_once(self, since: datetime.datetime | None = None) -> int:
        """Process one batch and return the count of extractions made."""
        return await self._process_batch(since_override=since)

    # ------------------------------------------------------------------ #

    def _filter_valid_entries(self, entries: list[LedgerEntry]) -> list[LedgerEntry]:
        """Keep only the user's own non-trivial messages; advance the watermark past the rest.

        Facts about the user are extracted from what the *user* wrote (``input``),
        never from agent/system output — the latter would let the pipeline learn
        the assistant's own text as user fact.

        The watermark only advances past the contiguous *prefix* of skipped
        entries. Skipped entries that come after a kept entry must not move it:
        the watermark is a single timestamp, so jumping past a newer skipped
        entry would also jump past any older kept entry whose extraction later
        fails, silently dropping its retry.
        """
        valid: list[LedgerEntry] = []
        for entry in entries:
            message = (entry.input or "").strip()
            skip = (
                entry.source not in _USER_AUTHORED_SOURCES
                or entry.status in _SKIPPED_STATUSES
                or len(message) < self._min_input_chars
            )
            if not skip:
                valid.append(entry)
            elif not valid:
                self._save_watermark(entry.timestamp)
        return valid

    async def _run_extractions_concurrently(self, entries: list[LedgerEntry]) -> list[bool | Exception]:
        """Perform concurrent extraction checks with a semaphore rate-limit cap."""
        sem = asyncio.Semaphore(self._max_concurrent)
        results: list[bool | Exception] = [False] * len(entries)

        async def _bounded(idx: int, entry: LedgerEntry) -> None:
            async with sem:
                try:
                    results[idx] = await self._process_entry(entry)
                except Exception as exc:
                    results[idx] = exc

        await asyncio.gather(*[_bounded(i, e) for i, e in enumerate(entries)])
        return results

    def _process_extraction_results(self, entries: list[LedgerEntry], results: list[bool | Exception]) -> int:
        """Process extraction output, saving watermarks sequentially until any error."""
        extractions = 0
        for entry, result in zip(entries, results, strict=True):
            if isinstance(result, Exception):
                logger.exception(
                    "ExtractionPipeline: failed on entry %s — watermark NOT advanced past this point, will retry",
                    entry.id,
                )
                break
            if result:
                extractions += 1
            self._save_watermark(entry.timestamp)

        return extractions

    async def _process_batch(self, since_override: datetime.datetime | None = None) -> int:
        async with self._lock:
            return await self._process_batch_locked(since_override)

    async def _process_batch_locked(self, since_override: datetime.datetime | None = None) -> int:
        try:
            metrics = await self._ledger.get_metrics(days=1)
            daily_cost = metrics.get("total_cost_usd", 0.0)
            if daily_cost >= self._max_daily_cost:
                logger.info(
                    "ExtractionPipeline: daily cost cap $%.2f reached (used $%.4f), skipping",
                    self._max_daily_cost,
                    daily_cost,
                )
                return 0
        except Exception:
            pass  # don't block extraction if metrics query fails

        await self._maybe_backup()
        since = since_override or self._load_watermark()
        entries = await self._ledger.query(LedgerFilters(since=since, limit=_BATCH_SIZE))
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
        """Ask the LLM whether the user's message yields a fact worth storing."""
        prompt = _EXTRACTION_PROMPT.format(message=(entry.input or "").strip()[:2000])

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

        # Deduplication: skip if the fact is already captured in the document.
        if await self._is_duplicate(doc, delta, entry.task_id):
            return False

        await self._context_store.append(doc, delta)

        if self._fact_store is not None:
            try:
                await self._fact_store.add_fact(delta, doc_key)
            except Exception:
                logger.warning("FactStore: failed to persist fact '%s'", delta[:80])

        # Trim document if it has grown too large.
        await self._maybe_trim(doc, entry.task_id)

        await self._ledger.write(
            LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=entry.task_id,
                action=f"extraction: {doc.value} updated",
                output=delta,
                status=LedgerStatus.COMPLETED,
            )
        )
        return True

    async def _is_duplicate(self, doc: ContextDocument, delta: str, task_id: str | None) -> bool:
        """Return True if delta is already captured in the document."""
        try:
            existing = await self._context_store.read(doc)
        except Exception:
            return False
        if not existing or len(existing) < 20:
            return False

        # Fast path: if fewer than 3 meaningful words overlap, it can't be a
        # duplicate — skip the LLM call entirely.
        key_words = {w.lower() for w in delta.split() if len(w) > 4}
        existing_lower = existing.lower()
        overlap = sum(1 for w in key_words if w in existing_lower)
        # Require at least 3 overlapping words AND at least 2/3 of key words —
        # a higher bar than the old max(2, 1/2) so the LLM is only called for
        # genuinely ambiguous near-duplicates.
        if key_words and overlap >= max(3, len(key_words) * 2 // 3):
            prompt = _DEDUP_PROMPT.format(existing=existing[-2000:], delta=delta)
            try:
                resp = await self._inference_router.complete(
                    CompletionRequest(
                        prompt=prompt,
                        priority=PoolPriority.LOW,
                        component="extraction_pipeline:dedup",
                        task_id=task_id,
                        json_mode=True,
                        max_tokens=20,
                    )
                )
                return bool(json.loads(resp.text.strip()).get("duplicate", False))
            except Exception:
                return False
        return False

    async def _maybe_trim(self, doc: ContextDocument, task_id: str | None) -> None:
        """Summarise and rewrite the document if it exceeds the size cap."""
        try:
            existing = await self._context_store.read(doc)
        except Exception:
            return
        if not existing or len(existing) <= _MAX_DOCUMENT_CHARS:
            return

        # Archive the pre-trim snapshot so condensation is always reversible.
        try:
            ts = utcnow().strftime("%Y%m%dT%H%M%S")
            await asyncio.to_thread(self._write_archive, doc, existing, ts)
        except Exception:
            logger.warning("ExtractionPipeline: failed to archive %s before trim", doc.value)

        prompt = _TRIM_PROMPT.format(content=existing, doc_type=doc.value)
        try:
            resp = await self._inference_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.LOW,
                    component="extraction_pipeline:trim",
                    task_id=task_id,
                    max_tokens=1024,
                )
            )
            trimmed = resp.text.strip()
            if trimmed and len(trimmed) < len(existing):
                await self._context_store.write(doc, trimmed)
                logger.info(
                    "ExtractionPipeline: trimmed %s from %d → %d chars",
                    doc.value,
                    len(existing),
                    len(trimmed),
                )
        except Exception:
            logger.warning("ExtractionPipeline: trim failed for %s", doc.value)

    def _write_archive(self, doc: ContextDocument, content: str, ts: str) -> None:
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_dir / f"{doc.value}.{ts}.bak"
        archive_path.write_text(content, encoding="utf-8")

    async def _maybe_backup(self) -> None:
        """Copy all context documents to a backup directory once per day."""
        stamp_path = self._backup_dir / ".last_backup"
        try:
            if stamp_path.exists():
                last = datetime.datetime.fromisoformat(stamp_path.read_text(encoding="utf-8").strip())
                if (utcnow() - last).total_seconds() < _BACKUP_INTERVAL_HOURS * 3600:
                    return
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._write_backup)
        except Exception:
            logger.warning("ExtractionPipeline: context backup failed", exc_info=True)

    def _write_backup(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        context_dir = self._watermark_path.parent
        for path in context_dir.glob("*.md"):
            dest = self._backup_dir / path.name
            shutil.copy2(path, dest)
        stamp_path = self._backup_dir / ".last_backup"
        stamp_path.write_text(utcnow().isoformat(), encoding="utf-8")

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
