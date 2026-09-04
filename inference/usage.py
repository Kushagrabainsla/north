"""Reading cache usage out of a provider's reply.

Every provider reports the same fact - "how much of this prompt did you already
have?" - under a different key, so each one is read here rather than in five
providers that would drift apart.

Why north measures this: the failure mode is silent. A cache is a *prefix*
match, so one changed byte near the front - a timestamp, a re-ordered tool list
- throws the whole thing away and the bill goes back to full price with nothing
logged and no error raised. The only symptom is this number falling to zero.

And zero is what it read, for a reason that was not the one assumed. The Codex
backend does cache and does report it: two identical 2412-token prompts came
back cold, then with 1792 tokens reused. What defeated it was north's own
prefix - a minute-resolution clock at the head of the system prompt, and a tool
list re-sorted by a confidence score that moves on every call. Both are fixed
in `agents/agentic_llm_agent.py`; this number is how you would know if they
regressed.
"""

from __future__ import annotations

from typing import Any

# Where each provider family puts the count of prompt tokens served from cache.
# ``None`` for the nested key means the value sits directly on ``usage``.
_CACHE_READ_KEYS: tuple[tuple[str | None, str], ...] = (
    ("prompt_tokens_details", "cached_tokens"),  # OpenAI-compatible: OpenRouter, Groq, Zen, Gemini
    ("input_tokens_details", "cached_tokens"),  # OpenAI Responses API, incl. the Codex backend
    (None, "cache_read_input_tokens"),  # Anthropic
    (None, "cached_tokens"),
)

# ...and where they report tokens *written* to the cache, which is billed at a
# premium and is how a first call pays for the ones after it.
_CACHE_WRITE_KEYS: tuple[tuple[str | None, str], ...] = (
    ("input_tokens_details", "cache_write_tokens"),  # OpenAI Responses API
    ("prompt_tokens_details", "cache_write_tokens"),
    (None, "cache_creation_input_tokens"),  # Anthropic
)


def _first_int(usage: dict[str, Any], keys: tuple[tuple[str | None, str], ...]) -> int:
    for parent, key in keys:
        container = usage if parent is None else usage.get(parent)
        if not isinstance(container, dict):
            continue
        value = container.get(key)
        if isinstance(value, int | float):
            return int(value)
    return 0


def cache_tokens(usage: dict[str, Any] | None) -> tuple[int, int]:
    """Return ``(read, written)`` cache token counts from a provider's usage block.

    Both are 0 when the provider says nothing about caching - which is not the
    same as "the cache missed", and is why the two are reported rather than
    inferred from the prompt size.
    """
    if not isinstance(usage, dict):
        return (0, 0)
    return (_first_int(usage, _CACHE_READ_KEYS), _first_int(usage, _CACHE_WRITE_KEYS))
