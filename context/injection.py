"""Manual context injection. See README Section 5.5."""

from __future__ import annotations

import asyncio
import json
import logging

from context.base import ContextStore
from context.models import ContextDocument
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from ledger.base import LedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from utils.ids import generate_id
from utils.text import strip_html
from utils.time import utcnow

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 4000

_DOCUMENT_MAP: dict[str, ContextDocument] = {
    "public": ContextDocument.PUBLIC,
    "private": ContextDocument.PRIVATE,
    "privacy_rules": ContextDocument.PRIVACY_RULES,
    "judgement_rules": ContextDocument.JUDGEMENT_RULES,
    "north_stars": ContextDocument.NORTH_STARS,
}

_ROUTING_PROMPT = """\
You are routing user-supplied context into the correct document for a personal AI system.

The five documents are:
- public: general non-sensitive facts (schedule, preferences, career, background)
- private: sensitive details (account numbers, medical, personal relationships)
- privacy_rules: rules governing which agents can access which data
- judgement_rules: decision patterns and learned preferences
- north_stars: goals across time horizons (lifetime, 5-year, 1-year, 3-month, this week)

The user has provided this content:

{content}

Decide which document this belongs to and write a concise delta to append (1–3 sentences maximum).
The delta should capture only the essential new facts — do not copy the original content verbatim.
Preserve key specifics: names, numbers, dates, deadlines, and thresholds.
A vague summary is less useful than a precise one.
Reply with JSON only:
{{"document": "<public|private|privacy_rules|judgement_rules|north_stars>", "delta": "<1-3 sentences>"}}
"""


class ContextInjector:
    """Accepts text, file bytes, or a URL and adds the content to the context layer.

    Uses a low-priority LLM call to decide which of the five documents to write
    to and what delta to append. All injections are logged to the Ledger with
    source=manual_injection.
    """

    def __init__(
        self,
        context_store: ContextStore,
        inference_router: InferenceRouter,
        ledger: LedgerWriter,
    ) -> None:
        self._context_store = context_store
        self._inference_router = inference_router
        self._ledger = ledger

    async def inject_text(self, text: str, task_id: str | None = None) -> ContextDocument:
        """Ingest raw text into the appropriate context document."""
        return await self._ingest(text, source_hint="text", task_id=task_id)

    async def inject_file(
        self,
        filename: str,
        content: bytes,
        task_id: str | None = None,
    ) -> ContextDocument:
        """Ingest a file. PDF and docx are parsed; everything else decoded as UTF-8."""
        lower = filename.lower()
        if lower.endswith(".pdf"):
            text = await asyncio.to_thread(_extract_pdf, content)
        elif lower.endswith(".docx"):
            text = await asyncio.to_thread(_extract_docx, content)
        else:
            text = content.decode("utf-8", errors="replace")
        return await self._ingest(text, source_hint=f"file:{filename}", task_id=task_id)

    async def inject_url(self, url: str, task_id: str | None = None) -> ContextDocument:
        """Fetch a URL (SSRF-guarded — public hosts only) and ingest its text content."""
        from utils.net import fetch_url_text

        fetched = await asyncio.to_thread(fetch_url_text, url, timeout=30.0)
        text = strip_html(fetched.text) if "html" in fetched.content_type else fetched.text

        return await self._ingest(text, source_hint=f"url:{url}", task_id=task_id)

    # ------------------------------------------------------------------ #

    async def _ingest(self, text: str, source_hint: str, task_id: str | None) -> ContextDocument:
        self._fire_ledger(
            LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.MANUAL_INJECTION,
                task_id=task_id,
                action="context_injection_started",
                input=source_hint,
                status=LedgerStatus.PENDING,
            )
        )

        doc, delta = await self._route(text)
        await self._context_store.append(doc, delta)

        self._fire_ledger(
            LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.MANUAL_INJECTION,
                task_id=task_id,
                action=f"context_injection_completed: {doc.value}",
                input=source_hint,
                output=delta,
                status=LedgerStatus.COMPLETED,
            )
        )
        return doc

    def _fire_ledger(self, entry: LedgerEntry) -> None:
        """Schedule a ledger write as a background task, logging failures."""
        task = asyncio.create_task(self._ledger.write(entry))
        task.add_done_callback(
            lambda t: (
                logger.warning("Ledger write failed: %s", t.exception())
                if not t.cancelled() and t.exception() is not None
                else None
            )
        )

    async def _route(self, text: str) -> tuple[ContextDocument, str]:
        """Ask the LLM which document this text belongs to and extract a delta."""
        response = await self._inference_router.complete(
            CompletionRequest(
                prompt=_ROUTING_PROMPT.format(content=text[:_MAX_CONTENT_CHARS]),
                priority=PoolPriority.LOW,
                component="context_injector",
                json_mode=True,
            )
        )
        try:
            result = json.loads(response.text.strip())
            doc_key = result["document"]
            delta = str(result["delta"]).strip()
            doc = _DOCUMENT_MAP.get(doc_key, ContextDocument.PUBLIC)
            return doc, delta
        except (json.JSONDecodeError, KeyError, ValueError):
            return ContextDocument.PUBLIC, text[:_MAX_CONTENT_CHARS]


def _extract_pdf(content: bytes) -> str:
    import io

    from pypdf import PdfReader  # type: ignore[import-untyped]

    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_docx(content: bytes) -> str:
    import io

    from docx import Document  # type: ignore[import-untyped]

    doc = Document(io.BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs).strip()
