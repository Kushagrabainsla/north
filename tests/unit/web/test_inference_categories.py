"""Dashboard model-call aggregation keeps internal and agent work separate."""

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from web.api import _inference_categories


def _call(category: str, component: str, *, tokens: int, cost: float) -> LedgerEntry:
    return LedgerEntry.new(
        source=LedgerSource.INFERENCE_ROUTER,
        task_id="t1",
        agent=category,
        action="inference_call",
        output=component,
        model_used="model-a",
        tokens_in=tokens,
        tokens_out=10,
        cached_tokens=5,
        cost_usd=cost,
        status=LedgerStatus.COMPLETED,
    )


def test_inference_categories_aggregate_each_purpose_independently() -> None:
    rows = _inference_categories(
        [
            _call("agent", "researcher", tokens=100, cost=0.1),
            _call("planning", "planner", tokens=50, cost=0.02),
            _call("agent", "coder", tokens=200, cost=0.2),
        ]
    )

    assert [row["category"] for row in rows] == ["planning", "agent"]
    assert rows[0]["calls"] == 1
    assert rows[1]["calls"] == 2
    assert rows[1]["tokens_in"] == 300
    assert rows[1]["components"] == ["coder", "researcher"]
