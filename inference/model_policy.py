"""Curated model preferences and matching helpers for ModelDispatcher.

By default the dispatcher ranks the whole provider catalog by a *price-derived*
quality score (see ``inference/capability.py``). For a coding task that makes
"which model writes the code" essentially the priciest model that is currently
up - shuffled among near-equal peers, and free to change between steps of one
task. That caps precision, unlike Claude Code / Cursor / opencode, which use a
known-strong, consistent model.

This module lets a small, ordered, editable list of known-strong models be
*preferred* per pool (``reasoning`` / ``fast_cheap``). The dispatcher tries the
available, healthy preferred models first and keeps the price-ranked catalog as
a resilient fallback, so a stale or unavailable preference never blocks a call.

Everything here is pure data/logic - the stateful promotion and per-task
stickiness live in ``ModelDispatcher``, which owns the registry, cooldowns, and
per-model success EMA. See docs/CODING_STYLE.md Sections 5.4, 6.5.
"""

from __future__ import annotations

import re

# Ordered, best-first, per pool. Entries are matched as *families*: every
# alphanumeric word in the entry must appear in a candidate's model id
# (case-insensitive), so "claude-sonnet" matches "anthropic/claude-sonnet-5",
# "gemini-2.5-pro" matches BOTH "google/gemini-2.5-pro" (OpenRouter) and
# "models/gemini-2.5-pro" (the Gemini direct provider), etc. Tokens are
# intentionally PROVIDER-AGNOSTIC (no "vendor/" prefix): the same model family
# is exposed under different ids by different providers (OpenRouter uses
# "google/gemini-...", the Gemini API uses "models/gemini-...", Groq uses bare
# or "meta-llama/..." ids), so a "google/gemini-2.5-pro" token would silently
# miss the very provider that can actually serve it. Prefix an entry with
# "provider:" to pin it (e.g. "openrouter:anthropic/claude-sonnet"). `high_volume`
# is intentionally absent: the cheapest-first pool (and the ECO strategy that
# maps onto it) must never be overridden by a preference for a pricier model.
DEFAULT_PREFERRED_MODELS: dict[str, list[str]] = {
    "reasoning": [
        "claude-sonnet",
        "claude-opus",
        "gpt-5",
        "gpt-4.1",
        "gemini-2.5-pro",
        "deepseek-chat",
        "qwen3-coder",
        "qwen-2.5-coder",
        "llama-4-scout",
    ],
    "fast_cheap": [
        "claude-haiku",
        "gpt-5-mini",
        "gpt-4.1-mini",
        "gpt-4o-mini",
        "gemini-2.5-flash",
        "gemini-flash",
        "llama-3.1-8b",
    ],
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9.]+")


def _normalize_id(text: str) -> str:
    """Lowercase and collapse runs of separators to a single '-' for matching.

    Keeps '.' (so "2.5" stays intact) and turns '/', '-', ':', spaces, etc. into
    a single '-', so a family token can be matched as a contiguous substring
    regardless of the provider's separator/prefix conventions.
    """
    return _NORMALIZE_RE.sub("-", text.lower()).strip("-")


def parse_preferred(raw: object) -> dict[str, list[str]]:
    """Coerce a settings/env value into a ``{pool: [specs]}`` map, dropping junk.

    Accepts a mapping whose values are either a list of specs or a single
    comma-separated string. Anything unparseable yields an empty map so a bad
    override degrades to the price-ranked default rather than raising.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for pool, val in raw.items():
        if not isinstance(pool, str):
            continue
        if isinstance(val, str):
            specs = [s.strip() for s in val.split(",")]
        elif isinstance(val, list | tuple):
            specs = [str(s).strip() for s in val]
        else:
            continue
        specs = [s for s in specs if s]
        if specs:
            out[pool] = specs
    return out


def split_spec(spec: str) -> tuple[str | None, str]:
    """Split ``"provider:token"`` into ``(provider, token)``.

    A bare token returns ``(None, token)``. Only a leading ``name:`` whose name
    has no ``/`` is treated as a provider qualifier, so OpenRouter ids (which
    contain ``/``, e.g. ``anthropic/claude-...``) are never mistaken for one.
    """
    head, sep, tail = spec.partition(":")
    if sep and "/" not in head and tail:
        return head.strip().lower(), tail.strip()
    return None, spec.strip()


def model_matches(spec: str, provider_name: str, model_id: str) -> bool:
    """True when *spec* selects the given ``(provider_name, model_id)``.

    Family match: the spec's token, normalized, must appear as a **contiguous
    substring** of the normalized model id, so "claude-sonnet" matches
    "anthropic/claude-sonnet-5" and "gemini-2.5-pro" matches both
    "google/gemini-2.5-pro" and "models/gemini-2.5-pro" - but "gpt-5" does NOT
    match "gpt-3.5-turbo" (which an independent-words match would wrongly accept
    because "5" occurs inside "3.5"). An optional "provider:" prefix must equal
    *provider_name*.
    """
    want_provider, token = split_spec(spec)
    if want_provider is not None and want_provider != provider_name.lower():
        return False
    ntoken = _normalize_id(token)
    return bool(ntoken) and ntoken in _normalize_id(model_id)
