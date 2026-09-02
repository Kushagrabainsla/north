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
    """Current model pool state."""
    pools = _get_inference_router().current_pools()
    return {name: ModelPoolOut(name=pool.name, models=pool.models) for name, pool in pools.items()}


