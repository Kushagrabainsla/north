"""OpenRouter's catalog as facts.

``GET /models`` is the richest single source north has: it declares context
window, price, which parameters a model accepts (tools, reasoning, structured
output), its input modalities, and - for the subset Artificial Analysis has
measured - coding, agentic and intelligence indices. Those measurements are the
whole point: they are what replaces a hand-written tier table.

``GET /models/{id}/endpoints`` is a second, per-model call describing the
upstream providers OpenRouter itself routes to. north cannot address one of
those directly, so it is used to enrich the OpenRouter endpoint row - best
uptime, cheapest upstream price, and any quantization disagreement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from inference.facts.identity import canonical, variant_of
from inference.facts.merge import quantization_mismatch
from inference.facts.models import Endpoint, ModelFacts, Rank, fact

SOURCE = "openrouter"
# Artificial Analysis publishes its indices on a 0-100 scale; facts hold 0-1.
_INDEX_SCALE = 100.0

# Parameters whose presence declares a capability. OpenRouter lists exactly what
# a model accepts, so absence is a declaration too - see merge.py.
_TOOL_PARAMS = ("tools", "tool_choice")
_REASONING_PARAMS = ("reasoning", "include_reasoning", "reasoning_effort")
_STRUCTURED_PARAMS = ("structured_outputs", "response_format")


def _price(pricing: dict, key: str) -> float | None:
    """A published price, or None when there is not one.

    A negative value is OpenRouter's way of saying "varies" on its meta-routers.
    Read literally it made them the cheapest models in the catalog and put them at
    the head of every cost-ranked chain, so it is treated as unknown.
    """
    try:
        value = pricing.get(key)
        if value is None:
            return None
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _index(benchmarks: dict, key: str, when: datetime) -> Any:
    scores = benchmarks.get("artificial_analysis")
    if not isinstance(scores, dict):
        return None
    raw = scores.get(key)
    if not isinstance(raw, int | float):
        return None
    return fact(float(raw) / _INDEX_SCALE, Rank.DECLARED, f"{SOURCE}.artificial_analysis", when)


def facts_from_catalog(raw_models: list[dict]) -> tuple[list[ModelFacts], list[Endpoint]]:
    """Parse a ``/models`` payload into fact records and endpoint rows.

    ``:free`` and ``:batch`` ids are endpoint variants of a model already in the
    list - same weights, different price and limits - so they collapse onto one
    canonical fact record rather than being dropped outright, which is what used
    to discard the free tier's facts along with them.

    Only ``:batch`` is held back from becoming a *callable* endpoint: it is an
    asynchronous API north has no path for yet. Its facts still count; it simply
    cannot be dialled, so offering it as an endpoint would only produce a 400 on
    the cheapest row of a chain.
    """
    when = datetime.now(UTC)
    facts: list[ModelFacts] = []
    endpoints: list[Endpoint] = []
    for model in raw_models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        canonical_id = canonical(model_id)
        if not canonical_id:
            continue
        params = {str(p) for p in (model.get("supported_parameters") or [])}
        architecture = model.get("architecture") or {}
        modalities = frozenset(str(m) for m in (architecture.get("input_modalities") or []))
        outputs = frozenset(str(m) for m in (architecture.get("output_modalities") or []))
        benchmarks = model.get("benchmarks") or {}
        top_provider = model.get("top_provider") or {}
        context_length = model.get("context_length") or top_provider.get("context_length")
        max_output = top_provider.get("max_completion_tokens")

        facts.append(
            ModelFacts(
                canonical_id=canonical_id,
                context_window=(
                    fact(int(context_length), Rank.DECLARED, SOURCE, when) if context_length else None
                ),
                max_output_tokens=(fact(int(max_output), Rank.DECLARED, SOURCE, when) if max_output else None),
                # A model that does not emit text is not a completion model,
                # however cheap it is.
                supports_completion=(
                    fact("text" in outputs, Rank.DECLARED, SOURCE, when) if outputs else None
                ),
                supports_tools=fact(
                    any(p in params for p in _TOOL_PARAMS), Rank.DECLARED, SOURCE, when
                ),
                supports_reasoning=fact(
                    any(p in params for p in _REASONING_PARAMS) or bool(model.get("reasoning")),
                    Rank.DECLARED,
                    SOURCE,
                    when,
                ),
                supports_structured=fact(
                    any(p in params for p in _STRUCTURED_PARAMS), Rank.DECLARED, SOURCE, when
                ),
                input_modalities=(fact(modalities, Rank.DECLARED, SOURCE, when) if modalities else None),
                coding_score=_index(benchmarks, "coding_index", when),
                agentic_score=_index(benchmarks, "agentic_index", when),
                intelligence_score=_index(benchmarks, "intelligence_index", when),
            )
        )
        if variant_of(model_id) == "batch":
            continue
        pricing = model.get("pricing") or {}
        endpoints.append(
            Endpoint(
                canonical_id=canonical_id,
                provider=SOURCE,
                provider_model_id=model_id,
                price_in=_price(pricing, "prompt"),
                price_out=_price(pricing, "completion"),
                context_window=int(context_length) if context_length else None,
            )
        )
    return facts, endpoints


def enrich_from_endpoints(endpoint: Endpoint, payload: dict) -> Endpoint:
    """Fold a ``/models/{id}/endpoints`` payload into *endpoint*.

    OpenRouter fans one model out across upstream providers that north cannot
    address individually, so the useful signal is the aggregate: the cheapest
    price actually on offer, the best recent uptime, and whether the upstreams
    disagree about quantization (recorded, not silently merged).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = (data or {}).get("endpoints") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return endpoint

    upstreams = [
        Endpoint(
            canonical_id=endpoint.canonical_id,
            provider=SOURCE,
            provider_model_id=endpoint.provider_model_id,
            price_in=_price(row.get("pricing") or {}, "prompt"),
            price_out=_price(row.get("pricing") or {}, "completion"),
            quantization=row.get("quantization"),
            uptime=row.get("uptime_last_30m"),
        )
        for row in rows
        if isinstance(row, dict)
    ]
    if not upstreams:
        return endpoint

    cheapest = min(upstreams, key=lambda e: e.price)
    uptimes = [e.uptime for e in upstreams if isinstance(e.uptime, int | float)]
    quantizations = sorted({e.quantization for e in upstreams if e.quantization})
    return Endpoint(
        canonical_id=endpoint.canonical_id,
        provider=endpoint.provider,
        provider_model_id=endpoint.provider_model_id,
        price_in=cheapest.price_in if cheapest.price_in is not None else endpoint.price_in,
        price_out=cheapest.price_out if cheapest.price_out is not None else endpoint.price_out,
        quantization=("mixed:" + ",".join(quantizations)) if quantization_mismatch(upstreams) else (
            quantizations[0] if quantizations else endpoint.quantization
        ),
        max_payload_chars=endpoint.max_payload_chars,
        entitlement=endpoint.entitlement,
        uptime=max(uptimes) if uptimes else endpoint.uptime,
        context_window=endpoint.context_window,
    )
