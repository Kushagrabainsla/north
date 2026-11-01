"""Usage totals count each model call once.

North writes one ledger row per model call, then repeats the cost and tokens of
those calls on the agent's row and the task's row. Totals summed every row, so a
chat task whose two calls cost $0.39534 was reported as $1.0692: the dashboard's
Usage tile, `north metrics` and the System page's tokens all disagreed with the
calls they claimed to add up, and memory learning compared that inflated figure
with its daily cap and switched itself off after the first task.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledger import INFERENCE_CALL, LedgerEntry, LedgerSource, LedgerStatus, LedgerWriter, SQLiteLedgerWriter
from ledger.base import LedgerFilters

# The first chat task of the test run, as north wrote it.
_PLANNER = {"cost_usd": 0.11684, "tokens_in": 11_525, "tokens_out": 159}
_AGENT = {"cost_usd": 0.2785, "tokens_in": 27_570, "tokens_out": 280}
_CALLS_COST = _PLANNER["cost_usd"] + _AGENT["cost_usd"]


def _rows(task_id: str = "t1", run_id: str = "run-1") -> list[LedgerEntry]:
    def row(**fields) -> LedgerEntry:
        return LedgerEntry.new(task_id=task_id, status=LedgerStatus.COMPLETED, **fields)

    return [
        row(
            source=LedgerSource.INFERENCE_ROUTER,
            agent="planning",
            action=INFERENCE_CALL,
            output="planner",
            model_used="claude-haiku-5-5",
            **_PLANNER,
        ),
        row(
            source=LedgerSource.INFERENCE_ROUTER,
            run_id=run_id,
            agent="agent",
            action=INFERENCE_CALL,
            output="general",
            model_used="claude-haiku-5-5",
            **_AGENT,
        ),
        # The agent's row and the task's row repeat those calls.
        row(source=LedgerSource.AGENT, run_id=run_id, agent="general", action="agent_completed", **_AGENT),
        row(source=LedgerSource.SYSTEM, action="task_completed", cost_usd=_CALLS_COST),
    ]


@pytest.fixture
async def ledger(tmp_path: Path) -> SQLiteLedgerWriter:
    writer = SQLiteLedgerWriter(tmp_path / "ledger.db")
    for entry in _rows():
        await writer.write(entry)
    return writer


async def test_metrics_add_up_each_call_once(ledger: SQLiteLedgerWriter) -> None:
    metrics = await ledger.get_metrics(days=7)

    assert metrics["total_cost_usd"] == pytest.approx(_CALLS_COST)
    assert metrics["total_tokens_in"] == _PLANNER["tokens_in"] + _AGENT["tokens_in"]
    assert metrics["total_tokens_out"] == _PLANNER["tokens_out"] + _AGENT["tokens_out"]
    assert metrics["by_model"] == {"claude-haiku-5-5": pytest.approx(_CALLS_COST)}
    assert metrics["total_tasks"] == 1


async def test_cost_breakdown_adds_up_each_call_once(ledger: SQLiteLedgerWriter) -> None:
    since = datetime.now(UTC) - timedelta(days=1)

    assert (await ledger.cost_breakdown(since=since))["total"] == pytest.approx(_CALLS_COST)
    by_router = await ledger.cost_breakdown(since=since, source=LedgerSource.INFERENCE_ROUTER)
    assert by_router["total"] == pytest.approx(_CALLS_COST)


async def test_an_agent_costs_its_own_calls_and_a_call_purpose_is_not_an_agent(ledger: SQLiteLedgerWriter) -> None:
    by_agent = {row["agent"]: row for row in (await ledger.get_metrics(days=7))["by_agent"]}

    assert set(by_agent) == {"general"}
    assert by_agent["general"]["cost_usd"] == pytest.approx(_AGENT["cost_usd"])
    assert by_agent["general"]["tasks"] == 1


async def test_a_call_made_outside_any_task_is_spent_all_the_same(ledger: SQLiteLedgerWriter) -> None:
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.INFERENCE_ROUTER,
            agent="memory",
            action=INFERENCE_CALL,
            output="episode_consolidator",
            model_used="claude-haiku-5-5",
            cost_usd=0.001,
            tokens_in=900,
            tokens_out=40,
            status=LedgerStatus.COMPLETED,
        )
    )
    since = datetime.now(UTC) - timedelta(days=1)

    metrics = await ledger.get_metrics(days=7)
    assert metrics["total_cost_usd"] == pytest.approx(_CALLS_COST + 0.001)
    assert metrics["total_cost_usd"] == pytest.approx((await ledger.cost_breakdown(since=since))["total"])


class _ListLedger(LedgerWriter):
    """The interface's own aggregation, over a list."""

    def __init__(self, entries: list[LedgerEntry]) -> None:
        self._entries = entries

    async def write(self, entry: LedgerEntry) -> str:
        self._entries.append(entry)
        return entry.id

    async def get(self, entry_id: str) -> LedgerEntry | None:
        return next((e for e in self._entries if e.id == entry_id), None)

    async def query(self, filters: LedgerFilters) -> list[LedgerEntry]:
        return list(self._entries)


async def test_the_interface_default_adds_up_each_call_once() -> None:
    breakdown = await _ListLedger(_rows()).cost_breakdown(since=datetime.now(UTC) - timedelta(days=1))

    assert breakdown["total"] == pytest.approx(_CALLS_COST)
    assert breakdown["by_component"] == {
        "planning": pytest.approx(_PLANNER["cost_usd"]),
        "agent": pytest.approx(_AGENT["cost_usd"]),
    }
