"""Inference cost and model-pool inspection."""

from __future__ import annotations

import datetime

from pydantic import BaseModel

from inference.models import CostSummary, ModelEntry
from ledger.models import LedgerSource
from orchestrator.api.deps import _get_inference_router, _get_ledger, router
from utils.time import utcnow


@router.get("/inference/costs", response_model=CostSummary)
async def inference_costs(
    period: str = "week",
    agent: str | None = None,
) -> CostSummary:
    """Aggregated inference costs over a period (day/week/month)."""
    now = utcnow()
    days = {"day": 1, "week": 7, "month": 30}.get(period, 7)
    since = now - datetime.timedelta(days=days)

    # Aggregation happens in the ledger (SQL GROUP BY for the SQLite store)
    # instead of summing up to 10k fetched rows here.
    breakdown = await _get_ledger().cost_breakdown(
        since=since,
        source=LedgerSource.INFERENCE_ROUTER,
        agent=agent,
    )

    return CostSummary(
        period=period,
        total_cost_usd=round(breakdown["total"], 6),
        by_component={k: round(v, 6) for k, v in breakdown["by_component"].items()},
        by_model={k: round(v, 6) for k, v in breakdown["by_model"].items()},
    )


class ModelPoolOut(BaseModel):
    name: str
    models: list[ModelEntry]


@router.get("/inference/models", response_model=dict[str, ModelPoolOut])
async def inference_models() -> dict[str, ModelPoolOut]:
    """Current model pool state. A catalog view only - pools select nothing."""
    pools = _get_inference_router().current_pools()
    return {name: ModelPoolOut(name=pool.name, models=pool.models) for name, pool in pools.items()}


class ChainModelOut(BaseModel):
    """One rung of a chain, and whether north could call it right now."""

    model: str
    score: float
    price: float | None = None
    providers: list[str] = []
    available: bool = True
    skipped_because: str = ""


class PartChainOut(BaseModel):
    """What one part of a task requires, and the models it would try in order."""

    part: str
    requires: list[str] = []
    order_by: str
    min_context: int = 0
    eligible: int
    models: list[ChainModelOut] = []


@router.get("/inference/chains", response_model=list[PartChainOut])
async def inference_chains(limit: int = 6) -> list[PartChainOut]:
    """How north actually picks a model: one ranked chain per part of a task.

    The pools endpoint above is a catalog view that selects nothing - a leftover
    grouping from the router that was deleted. This is the live thing: the
    chain, in walk order, for each part, honouring the routing mode, the power
    dial and any pinned model at the moment it is asked.
    """
    return [PartChainOut(**part) for part in _get_inference_router().part_chains(limit)]


class ProviderModelsOut(BaseModel):
    """Every model one provider serves, for choosing one by hand.

    Grouped by provider because that is the order the choice is made in: a person
    picks who they are talking to, then which model. A flat list of several
    hundred ids is not a choice anyone can make.
    """

    provider: str
    models: list[str]


@router.get("/inference/catalog", response_model=list[ProviderModelsOut])
async def inference_catalog() -> list[ProviderModelsOut]:
    """Reachable models, grouped by provider - what manual routing picks from."""
    by_provider: dict[str, set[str]] = {}
    for pool in _get_inference_router().current_pools().values():
        for entry in pool.models:
            by_provider.setdefault(entry.provider, set()).add(entry.id)
    return [
        ProviderModelsOut(provider=provider, models=sorted(models)) for provider, models in sorted(by_provider.items())
    ]
