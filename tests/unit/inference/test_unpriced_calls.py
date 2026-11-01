"""A call to a model north has no price for is recorded as unpriced, not as a guess or as free.

The Anthropic table charged an unlisted model at a $10/M estimate, so every
call to the 5.5 models (missing from it) carried a made-up cost into the
ledger, the totals and memory's budget. A call north cannot price records no
cost, and every view that shows a cost says how many calls it leaves out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, CompletionResponse
from ledger import INFERENCE_CALL, LedgerEntry, LedgerSource, LedgerStatus, SQLiteLedgerWriter
from tests.conftest import MockInferenceRouter
from web.api import _inference_categories


class _NewModelRouter(MockInferenceRouter):
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text="ok", model_used="claude-new-model", tokens_in=900, tokens_out=40, cost_usd=0.0, priced=False
        )


def _call(cost: float | None, model: str) -> LedgerEntry:
    return LedgerEntry.new(
        source=LedgerSource.INFERENCE_ROUTER,
        task_id="t1",
        agent="agent",
        action=INFERENCE_CALL,
        output="general",
        model_used=model,
        tokens_in=900,
        tokens_out=40,
        cost_usd=cost,
        status=LedgerStatus.COMPLETED,
    )


async def test_the_call_record_of_an_unpriced_call_has_no_cost() -> None:
    recorded: list[dict] = []

    async def sink(task_id: str | None, data: dict) -> None:
        recorded.append(data)

    tracker = CostTracker(_NewModelRouter(), call_sink=sink)
    await tracker.complete(CompletionRequest(prompt="hi", component="planner", task_id="t1"))

    assert recorded[0]["cost_usd"] is None
    assert tracker.pop_task_cost("t1") == 0.0


async def test_totals_count_unpriced_calls_instead_of_adding_them(tmp_path: Path) -> None:
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    await ledger.write(_call(0.0041, "claude-haiku-5-5"))
    await ledger.write(_call(None, "claude-new-model"))

    metrics = await ledger.get_metrics(days=7)
    breakdown = await ledger.cost_breakdown(since=datetime.now(UTC) - timedelta(days=1))

    assert metrics["total_cost_usd"] == pytest.approx(0.0041)
    assert metrics["unpriced_calls"] == 1
    assert breakdown["total"] == pytest.approx(0.0041)
    assert breakdown["unpriced_calls"] == 1


def test_a_tasks_model_calls_count_unpriced_calls() -> None:
    (row,) = _inference_categories([_call(0.0041, "claude-haiku-5-5"), _call(None, "claude-new-model")])

    assert row["calls"] == 2
    assert row["cost_usd"] == pytest.approx(0.0041)
    assert row["unpriced_calls"] == 1
