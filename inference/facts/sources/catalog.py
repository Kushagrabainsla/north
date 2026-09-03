"""Live provider catalogs as endpoint rows, plus the last-resort name heuristic.

A provider catalog answers one question the fetched sources cannot: *which
models can this account actually call, and under what id*. OpenCode Zen, for
instance, publishes only ``{id, object, created, owned_by}`` - no context
window, no capabilities, no price - so its entries become endpoint rows and
borrow their facts from OpenRouter and LiteLLM via the canonical id.

For the handful of models no source describes at all, capabilities are guessed
from the id. That guess is ranked :attr:`Rank.INFERRED`, so any real source
overrides it, and it never excludes a model - it only keeps one callable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id
from inference.facts.identity import canonical
from inference.facts.models import Endpoint, ModelFacts, Rank, fact

SOURCE = "provider_catalog"
INFERRED_SOURCE = "model_id_heuristic"


def endpoints_from_models(models: dict[str, ModelInfo]) -> list[Endpoint]:
    """One endpoint row per model a provider currently serves.

    Price and limits come from the serving provider or not at all - they are the
    one thing that must never be borrowed across providers. A provider that did
    not publish a price yields an endpoint with no price rather than the
    provider's internal stand-in, so an unpriced model is ranked by prior instead
    of by a number nobody measured.
    """
    rows: list[Endpoint] = []
    for model_id, info in models.items():
        canonical_id = canonical(model_id)
        if not canonical_id:
            continue
        price = info.cost_per_token if info.price_known else None
        rows.append(
            Endpoint(
                canonical_id=canonical_id,
                provider=info.provider_name,
                provider_model_id=model_id,
                price_in=price,
                price_out=price,
                max_payload_chars=info.max_payload_chars,
                context_window=info.context_window or None,
            )
        )
    return rows


def inferred_facts(models: dict[str, ModelInfo]) -> list[ModelFacts]:
    """Name-derived facts for models, ranked so any real source outranks them.

    This is the floor, not the plan: it is what keeps a model no catalog
    describes selectable rather than invisible.
    """
    when = datetime.now(UTC)
    out: list[ModelFacts] = []
    for model_id, info in models.items():
        canonical_id = canonical(model_id)
        if not canonical_id:
            continue
        caps = capabilities_from_model_id(model_id, info.provider_name)
        modalities = frozenset({"text", "image"} if ModelCapability.VISION in caps else {"text"})
        out.append(
            ModelFacts(
                canonical_id=canonical_id,
                context_window=(
                    fact(info.context_window, Rank.INFERRED, SOURCE, when) if info.context_window > 0 else None
                ),
                supports_completion=fact(
                    ModelCapability.COMPLETION in caps, Rank.INFERRED, INFERRED_SOURCE, when
                ),
                supports_tools=fact(
                    ModelCapability.TOOL_CALLS in caps, Rank.INFERRED, INFERRED_SOURCE, when
                ),
                supports_reasoning=fact(
                    ModelCapability.REASONING in caps, Rank.INFERRED, INFERRED_SOURCE, when
                ),
                # No source declares this and no name reliably implies it, so the
                # assumption is "probably yes, until a call proves otherwise" -
                # an OBSERVED contradiction is what settles it.
                supports_structured=fact(
                    ModelCapability.COMPLETION in caps, Rank.INFERRED, INFERRED_SOURCE, when
                ),
                input_modalities=fact(modalities, Rank.INFERRED, INFERRED_SOURCE, when),
            )
        )
    return out
