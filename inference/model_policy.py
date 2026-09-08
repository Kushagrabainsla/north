"""Naming a model: how a written spec is matched against a live catalog entry.

A person writes ``claude-sonnet`` or ``groq:qwen3-32b``; a provider serves
``anthropic/claude-sonnet-5`` or ``models/gemini-2.5-pro``. The same family is
spelled differently by every provider, so a spec is matched as a *family* rather
than compared for equality.

Everything here is pure: a spec, a provider name and a model id in, a boolean
out. Nothing knows what the match is for, which is why the same function serves
the chain's pinned model and, later, the user's manual model choice.
"""

from __future__ import annotations

import re

_NORMALIZE_RE = re.compile(r"[^a-z0-9.]+")


def _normalize_id(text: str) -> str:
    """Lowercase and collapse runs of separators to a single '-' for matching.

    Keeps '.' (so "2.5" stays intact) and turns '/', '-', ':', spaces, etc. into
    a single '-', so a family token can be matched as a contiguous substring
    regardless of the provider's separator/prefix conventions.
    """
    return _NORMALIZE_RE.sub("-", text.lower()).strip("-")


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
