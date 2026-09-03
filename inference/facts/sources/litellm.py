"""LiteLLM's price/context table as facts.

LiteLLM publishes one static JSON describing ~3,500 model entries across 40+
providers. It carries no quality scores, but it is the only source that
describes several models north most wants to use - the ``-codex`` line among
them - and it covers providers that publish nothing but a list of ids.

It is a single file that changes slowly, so it is fetched daily and cached on
disk. On any fetch error the cached copy is used: stale facts beat no facts,
and the fetch time travels with every fact so staleness stays visible.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from inference.facts.identity import canonical
from inference.facts.models import ModelFacts, Rank, fact

logger = logging.getLogger(__name__)

SOURCE = "litellm"
CATALOG_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
REFRESH_INTERVAL = timedelta(days=1)
_FETCH_TIMEOUT_SECONDS = 30.0

# Entries that are not chat/completion models. LiteLLM mixes image, audio,
# rerank and moderation models into the same file.
_CHAT_MODES = frozenset({"chat", "completion", "responses"})
# A spec template, not a model.
_SKIP_KEYS = frozenset({"sample_spec"})


def facts_from_catalog(raw: dict, when: datetime | None = None) -> list[ModelFacts]:
    """Parse the LiteLLM table into fact records, chat-capable entries only."""
    fetched_at = when or datetime.now(UTC)
    out: list[ModelFacts] = []
    for model_id, entry in raw.items():
        if model_id in _SKIP_KEYS or not isinstance(entry, dict):
            continue
        if str(entry.get("mode") or "") not in _CHAT_MODES:
            continue
        canonical_id = canonical(model_id)
        if not canonical_id:
            continue
        context_window = entry.get("max_input_tokens") or entry.get("max_tokens")
        max_output = entry.get("max_output_tokens")
        modalities = frozenset(str(m) for m in (entry.get("supported_modalities") or []))
        out.append(
            ModelFacts(
                canonical_id=canonical_id,
                context_window=(
                    fact(int(context_window), Rank.DECLARED, SOURCE, fetched_at) if context_window else None
                ),
                max_output_tokens=(
                    fact(int(max_output), Rank.DECLARED, SOURCE, fetched_at) if max_output else None
                ),
                supports_tools=fact(
                    bool(entry.get("supports_function_calling")), Rank.DECLARED, SOURCE, fetched_at
                ),
                supports_reasoning=fact(bool(entry.get("supports_reasoning")), Rank.DECLARED, SOURCE, fetched_at),
                supports_structured=fact(
                    bool(entry.get("supports_response_schema")), Rank.DECLARED, SOURCE, fetched_at
                ),
                input_modalities=(fact(modalities, Rank.DECLARED, SOURCE, fetched_at) if modalities else None),
            )
        )
    return out


class LiteLLMSource:
    """Daily fetch of the LiteLLM table, cached on disk and never fatal."""

    def __init__(self, cache_path: Path, *, client: httpx.AsyncClient | None = None) -> None:
        self._cache_path = cache_path
        self._client = client
        self._last_fetch: datetime | None = None

    def _is_due(self, now: datetime) -> bool:
        if self._last_fetch is None:
            return True
        return now - self._last_fetch >= REFRESH_INTERVAL

    def _read_cache(self) -> tuple[dict, datetime] | None:
        try:
            if not self._cache_path.exists():
                return None
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            when = datetime.fromtimestamp(self._cache_path.stat().st_mtime, tz=UTC)
            return (raw, when) if isinstance(raw, dict) else None
        except Exception:
            logger.warning("LiteLLM cache at %s is unreadable - ignoring it", self._cache_path, exc_info=True)
            return None

    def _write_cache(self, raw: dict) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError as exc:
            logger.warning("Could not cache the LiteLLM catalog to %s: %s", self._cache_path, exc)

    async def load(self) -> list[ModelFacts]:
        """Return LiteLLM facts, fetching only when the cached copy is a day old.

        Never raises: a source outage falls back to the cache, and no cache at
        all simply contributes no facts.
        """
        now = datetime.now(UTC)
        cached = self._read_cache()
        if cached is not None and not self._is_due(cached[1]):
            self._last_fetch = cached[1]
            return facts_from_catalog(cached[0], cached[1])

        try:
            client = self._client or httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
            try:
                response = await client.get(CATALOG_URL)
                response.raise_for_status()
                raw = response.json()
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception as exc:
            if cached is None:
                logger.warning("LiteLLM catalog unavailable and not cached (%s) - continuing without it", exc)
                return []
            logger.info("LiteLLM catalog fetch failed (%s) - using the cached copy from %s", exc, cached[1])
            self._last_fetch = cached[1]
            return facts_from_catalog(cached[0], cached[1])

        if not isinstance(raw, dict):
            return facts_from_catalog(cached[0], cached[1]) if cached else []
        self._write_cache(raw)
        self._last_fetch = now
        return facts_from_catalog(raw, now)
