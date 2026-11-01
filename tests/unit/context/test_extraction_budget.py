"""Memory's daily budget counts memory's own calls.

The extraction cap ($0.10 a day) was compared with north's whole daily spend.
One Sonnet task costs more than that, so in the issue #76 test learning stopped
after the first message of the day: "daily cost cap $0.10 reached (used
$1.0692), skipping", and "my dog is called biscuit" was never stored.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger import INFERENCE_CALL, SQLiteLedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import FileContextStore
from memory.extraction import COMPONENT, ExtractionPipeline
from utils.ids import generate_id
from utils.time import utcnow

_MESSAGE = "hi! quick intro: im sam, i work as a nurse at valley medical, and my dog is called biscuit"


class _Model:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def complete(self, request) -> SimpleNamespace:
        self.asked.append(request.prompt)
        return SimpleNamespace(text='{"facts": []}')


async def _write(ledger: SQLiteLedgerWriter, source: LedgerSource, task: str | None, **fields) -> None:
    await ledger.write(
        LedgerEntry(id=generate_id(), timestamp=utcnow() - timedelta(minutes=1), source=source, task_id=task, **fields)
    )


async def _call(ledger: SQLiteLedgerWriter, component: str, cost: float) -> None:
    await _write(
        ledger,
        LedgerSource.INFERENCE_ROUTER,
        None,
        agent="memory",
        action=INFERENCE_CALL,
        output=component,
        cost_usd=cost,
        status=LedgerStatus.COMPLETED,
    )


@pytest.fixture
async def setup(tmp_path: Path) -> tuple[SQLiteLedgerWriter, _Model, ExtractionPipeline]:
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    model = _Model()
    pipeline = ExtractionPipeline(
        ledger, FileContextStore(tmp_path / "context"), model, tmp_path, max_daily_cost_usd=0.10
    )
    await _write(
        ledger, LedgerSource.PROMPT, "t1", input=_MESSAGE, action="task_received", status=LedgerStatus.COMPLETED
    )
    await _write(ledger, LedgerSource.SYSTEM, "t1", action="task_completed", status=LedgerStatus.COMPLETED)
    return ledger, model, pipeline


async def test_north_spending_more_than_memorys_budget_does_not_stop_memory(setup) -> None:
    ledger, model, pipeline = setup
    # The task itself: a planner and an agent call, $1.07 between them, well over $0.10.
    for component, cost in (("planner", 0.40), ("general", 0.67)):
        await _write(
            ledger,
            LedgerSource.INFERENCE_ROUTER,
            "t1",
            agent="agent",
            action=INFERENCE_CALL,
            output=component,
            cost_usd=cost,
            status=LedgerStatus.COMPLETED,
        )
    await _call(ledger, COMPONENT, 0.02)

    await pipeline.run_once()

    assert len(model.asked) == 1
    assert "biscuit" in model.asked[0]


async def test_memory_stops_at_its_own_budget(setup) -> None:
    ledger, model, pipeline = setup
    await _call(ledger, COMPONENT, 0.06)
    await _call(ledger, f"{COMPONENT}:dedup", 0.05)

    await pipeline.run_once()

    assert model.asked == []


async def test_a_component_that_starts_with_the_same_name_is_not_memorys(setup) -> None:
    ledger, model, pipeline = setup
    await _call(ledger, f"{COMPONENT}_v2", 0.50)

    await pipeline.run_once()

    assert len(model.asked) == 1
