"""Builds and buckets inference model pools from OpenRouter's /models response.

Responsible for converting the raw API payload into the three-tier pool
structure (reasoning / fast_cheap / high_volume) plus a free_fallback pool.
"""
from __future__ import annotations

from inference.fallback_pools import FALLBACK_POOLS
from inference.models import ModelPool


def dedup(models: list[str]) -> list[str]:
    """Return models with duplicates removed, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def models_asc_from_pools(pools: dict[str, ModelPool]) -> list[str]:
    """Build a cheapest-first ordered list from pool structure (used as fallback)."""
    asc: list[str] = []
    for name in ("high_volume", "fast_cheap", "reasoning"):
        pool = pools.get(name)
        if pool:
            asc.extend(pool.models)
    return dedup(asc)


def output_price(model: dict) -> float:
    """Return the per-token completion price as a float, or 0 if unparseable."""
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return 0.0
    raw = pricing.get("completion", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def bucket_models(models: list[dict]) -> tuple[dict[str, ModelPool], list[str]]:
    """Bucket OpenRouter's /models response into pools by output cost.

    Returns (pools, all_priced_asc) where all_priced_asc is every priced model
    sorted cheapest-first — used by eco/sport strategy chains.
    """
    priced: list[tuple[str, float]] = []
    free_ids: list[str] = []

    for m in models:
        model_id = m.get("id")
        if not isinstance(model_id, str):
            continue
        price = output_price(m)
        if price <= 0:
            if model_id.endswith(":free"):
                free_ids.append(model_id)
        else:
            priced.append((model_id, price))

    static_free = list(FALLBACK_POOLS["free_fallback"].models)
    merged_free = static_free + [m for m in free_ids if m not in static_free]

    if not priced:
        pools = dict(FALLBACK_POOLS)
        return pools, models_asc_from_pools(pools)

    priced.sort(key=lambda pair: pair[1], reverse=True)
    n = len(priced)
    third = max(1, n // 3)

    reasoning_ids  = [mid for mid, _ in priced[:third]]
    fast_cheap_ids = [mid for mid, _ in priced[third: 2 * third]] or reasoning_ids
    high_volume_ids = [mid for mid, _ in priced[-third:]]

    all_priced_asc = [mid for mid, _ in reversed(priced)]

    pools = {
        "reasoning":    ModelPool(name="reasoning",    models=reasoning_ids),
        "fast_cheap":   ModelPool(name="fast_cheap",   models=fast_cheap_ids),
        "high_volume":  ModelPool(name="high_volume",  models=high_volume_ids),
        "free_fallback": ModelPool(name="free_fallback", models=merged_free),
    }
    return pools, all_priced_asc
