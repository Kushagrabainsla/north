"""Model capability taxonomy and per-model metadata used by ModelDispatcher."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from inference.constants import _FREE_MODEL_QUALITY, _QUALITY_LOG_MAX, _QUALITY_LOG_MIN


def quality_from_cost(cost_per_token: float) -> float:
    """Derive a 0–1 base_quality score from output token price.

    Log-scale normalisation spreads scores across the wide pricing range of
    available models (~$0.000001–$0.015/token).  Free models receive a fixed
    floor of _FREE_MODEL_QUALITY.  ModelDispatcher blends this score with a
    live per-model success-rate EMA when ranking candidates.
    """
    if cost_per_token <= 0:
        return _FREE_MODEL_QUALITY
    log_cost = math.log10(cost_per_token)
    normalised = (log_cost - _QUALITY_LOG_MIN) / (_QUALITY_LOG_MAX - _QUALITY_LOG_MIN)
    return max(0.0, min(normalised, 1.0))


class ModelState(StrEnum):
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    DEPLETED = "depleted"
    DEGRADED = "degraded"


class ModelCapability(StrEnum):
    COMPLETION = "completion"
    TOOL_CALLS = "tool_calls"
    EMBEDDING = "embedding"
    TRANSCRIPTION = "transcription"
    VISION = "vision"
    AUDIO = "audio"
    REASONING = "reasoning"
    SPEED = "speed"


def capabilities_from_model_id(model_id: str, provider_name: str = "") -> frozenset[ModelCapability]:
    """Infer capabilities from naming conventions in the model ID and provider context.

    Categorizes models into completion, tool calling, embeddings, transcription,
    vision/multimodal, deep reasoning, and high-speed tiers.
    """
    lower = model_id.lower()
    caps: set[ModelCapability] = set()

    if "whisper" in lower or "transcri" in lower:
        caps.add(ModelCapability.TRANSCRIPTION)
        caps.add(ModelCapability.AUDIO)
        return frozenset(caps)

    if "tts" in lower or "audio" in lower:
        caps.add(ModelCapability.AUDIO)
        return frozenset(caps)

    if "embed" in lower:
        caps.add(ModelCapability.EMBEDDING)
        return frozenset(caps)

    _NON_CHAT_PATTERNS = (
        "prompt-guard",
        "llama-guard",
        "orpheus",
        "-guard-",
        "imagen",
        "veo",
        "lyria",
        "robotics",
        "deep-research",
        "live-translate",
    )
    if any(kw in lower for kw in _NON_CHAT_PATTERNS):
        return frozenset()

    import re

    tokens = set(re.split(r"[-_./: ]+", lower))

    # Core completion and tool calling
    caps.add(ModelCapability.COMPLETION)
    caps.add(ModelCapability.TOOL_CALLS)

    # Multimodal / Vision
    alpha_variants = ["ox-alpha", "oxalpha", "0x-alpha", "0xalpha", "0x_alpha", "ox_alpha", "stealth"]
    vision_models = ["gemini", "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "sonnet", "opus", "haiku"]
    if (
        any(k in lower for k in ["vision", "-vl", "image", *alpha_variants])
        or any(t in tokens for t in vision_models)
    ):
        caps.add(ModelCapability.VISION)

    # Deep Reasoning / Complex Coding
    reasoning_tokens = ["sonnet", "opus", "gpt-5", "gpt-4o", "gpt-4.1", "pro", "r1", "coder", "70b", "405b", "120b"]
    reasoning_keywords = [
        "deepseek-r1", "deepseek-chat", "deepseek-v3", "qwen3-coder", "qwen-2.5-coder",
        "qwen3.6-27b", *alpha_variants,
    ]
    is_reasoning = any(t in tokens for t in reasoning_tokens) or any(k in lower for k in reasoning_keywords)
    is_mini_nano = (
        ("mini" in tokens and "gemini" not in tokens)
        or "nano" in tokens
        or "flash-lite" in lower
        or "guard" in lower
    )
    if is_reasoning and not is_mini_nano:
        caps.add(ModelCapability.REASONING)

    # High-Speed / Ultra-Fast TTFT
    is_speed = (
        any(t in tokens for t in ["flash", "haiku", "nano", "8b", "lite", "turbo"])
        or ("mini" in tokens and "gemini" not in tokens)
        or "compound-mini" in lower
        or (provider_name == "groq" and "70b" in tokens)
    )
    if is_speed and not any(t in tokens for t in ["guard", "embed", "whisper"]):
        caps.add(ModelCapability.SPEED)

    # Models without function/tool calling support
    if "compound" in lower:
        caps.discard(ModelCapability.TOOL_CALLS)

    return frozenset(caps)


@dataclass(frozen=True)
class ModelInfo:
    """Immutable descriptor for one model on one provider.

    base_quality is a 0–1 price-derived score.  ModelDispatcher blends it
    with a live in-memory success-rate EMA to produce effective_quality for
    candidate ranking.
    """

    model_id: str
    provider_name: str
    capabilities: frozenset[ModelCapability]
    context_window: int  # max tokens (input + output combined)
    cost_per_token: float  # USD per output token; 0.0 for free models
    base_quality: float  # 0.0–1.0 quality estimate
    # Max total request size (chars) this model accepts. None = unlimited.
    # Free-tier providers (e.g. Groq free) cap request size far below north's
    # system-prompt + context, so they 413 on normal prompts; marking a low cap
    # here lets the dispatcher route large prompts to models that accept them.
    max_payload_chars: int | None = None

    @property
    def is_free(self) -> bool:
        return self.cost_per_token == 0.0

    def supports(self, capability: ModelCapability) -> bool:
        return capability in self.capabilities
