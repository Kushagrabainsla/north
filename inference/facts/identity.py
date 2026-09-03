"""Canonical model identity - the join key across catalog sources and providers.

The same model reaches north under several ids: ``anthropic/claude-opus-5`` from
OpenRouter, ``anthropic.claude-opus-5`` from LiteLLM, bare ``claude-opus-5`` from
OpenCode Zen. Facts learned about one must apply to all three, so every id is
reduced to one canonical form before anything is merged or compared.

The rule that matters: **strip only known variant suffixes, never version or
size tokens.** ``gpt-5.1-codex`` and ``gpt-5.1-codex-max`` are different models,
and a wrong merge silently routes work to a model the caller did not choose.
Over-merging is worse than failing to merge, so this is deliberately
conservative - an unrecognised suffix is kept.
"""

from __future__ import annotations

import re

# Suffixes after a ":" that name an *endpoint* variant - same weights, different
# price or limits - rather than a different model. Anything not listed here is
# kept, so an unknown ":something" never silently merges two models.
VARIANT_SUFFIXES: frozenset[str] = frozenset(
    {
        "free",
        "beta",
        "extended",
        "nitro",
        "thinking",
        "batch",
        "preview",
        "exp",
        "latest",
        "floor",
        "online",
    }
)

# Vendor namespaces LiteLLM writes with a "." separator ("anthropic.claude-opus-5").
# A "/" separator is handled generically; only the dot form needs a name list,
# because a dot is also a version separator ("gemini-3.1-pro") and must not be
# split blindly.
VENDOR_PREFIXES: frozenset[str] = frozenset(
    {
        "ai21",
        "amazon",
        "anthropic",
        "cohere",
        "deepseek",
        "google",
        "meta",
        "meta-llama",
        "microsoft",
        "mistral",
        "moonshotai",
        "nvidia",
        "openai",
        "qwen",
        "us",
        "eu",
        "apac",
        "x-ai",
        "z-ai",
    }
)

# Trailing markers that name a release channel or an endpoint variant rather than
# a distinct model. "-free" is here because OpenCode Zen writes its free tier as a
# suffix ("glm-5-free") where OpenRouter writes it as a variant ("z-ai/glm-5:free");
# they are the same weights, and keeping them apart left Zen's entire free tier
# borrowing no facts from anyone.
_CHANNEL_SUFFIXES: tuple[str, ...] = ("-preview", "-latest", "-free")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _strip_vendor_dot_prefix(text: str) -> str:
    """Drop a leading ``vendor.`` namespace, keeping version dots intact.

    Only a prefix in :data:`VENDOR_PREFIXES` is removed. ``gemini-3.1-pro`` has a
    dot too, and splitting on it would turn every point release into a different
    model.
    """
    head, sep, tail = text.partition(".")
    if sep and tail and head in VENDOR_PREFIXES:
        return tail
    return text


def canonical(model_id: str) -> str:
    """Reduce a provider- or source-specific model id to its canonical identity.

    ``openai/gpt-5.1-codex``, ``gpt-5.1-codex`` and ``openai.gpt-5.1-codex`` all
    become ``gpt-5-1-codex``; ``gpt-5.1-codex-max`` stays distinct.
    """
    text = model_id.strip().lower()
    if not text:
        return ""
    text = text.split("/")[-1]  # drop the provider path
    text = _strip_vendor_dot_prefix(text)
    head, sep, tail = text.rpartition(":")
    if sep and tail in VARIANT_SUFFIXES:
        text = head
    text = _NON_ALNUM.sub("-", text).strip("-")
    for suffix in _CHANNEL_SUFFIXES:
        text = text.removesuffix(suffix)
    return text


def variant_of(model_id: str) -> str | None:
    """Return the endpoint-variant suffix of *model_id* (``free``, ``batch``), if any.

    The variant is dropped from the canonical id but is not noise: it is what
    distinguishes two *endpoints* of one model, and ``:free`` in particular is
    the whole free tier.
    """
    _, sep, tail = model_id.strip().lower().rpartition(":")
    return tail if sep and tail in VARIANT_SUFFIXES else None
