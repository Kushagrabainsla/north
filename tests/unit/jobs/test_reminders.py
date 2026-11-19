"""A reminder is a schedule of the built-in reminder flow: its text, delivered at its time, with a record.

In the issue #76 test each one-off reminder became a permanent flow of its own
that ran once as a "test" when it was approved (finding M17), and the run at its
time reached no one on a dashboard-only install while north had said "It will
tell you" (M4). One that came due while north was off ran later, saying nothing
of it (M18). Now every reminder runs the one reminder flow, whose single step
delivers the schedule's text as it is: no model, no test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.notices import NoticeStore
from approval.store import ApprovalStore
from flows import FlowRegistry
from flows.store import FlowRunStore
from jobs.cron_store import UserCronStore
from jobs.models import REMINDER_FLOW
from jobs.scheduler import CronEntry, CronScheduler
from ledger import SQLiteLedgerWriter
from ledger.base import LedgerFilters
from orchestrator.app import _deliver_reminder
from tests.conftest import approval_policy
from tests.unit.flows.test_runner import FakeAgent, FakeAgents, _skills
from tools.universal.flow_runner import FlowRunner
from tools.universal.run_flow import RunFlowTool

_BUILTIN = Path(__file__).parents[3] / "resources" / "builtin-flows"


class _Queue:
    async def enqueue(self, job) -> None:
        pass


@pytest.fixture
def home(tmp_path: Path) -> dict:
    """A dashboard-only install: no TUI, Telegram or macOS notifier, and no model."""
    notices = NoticeStore(tmp_path / "notices.db")
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    approvals = Approvals(approval_policy(), UserInteraction(ApprovalStore(), notices=notices, ledger=ledger))
    return {"notices": notices, "ledger": ledger, "approvals": approvals}


def test_the_reminder_flow_is_built_in_and_live() -> None:
    flow = FlowRegistry(builtin_dir=_BUILTIN, learned_dir=_BUILTIN / "none").get(REMINDER_FLOW)
    assert flow.status == "active"
    assert [step.action for step in flow.steps] == ["deliver_reminder"]


async def test_a_recurring_reminder_carries_its_flow_and_message_to_every_firing(tmp_path) -> None:
    cron = UserCronStore(tmp_path / "jobs.db")
    await cron.add(
        name="stretch", agent="north", task="Stretch", hour=15, minute=0, weekdays=None,
        label="Stretch", flow=REMINDER_FLOW, message="Stand up and stretch.",
    )  # fmt: skip
    [row] = await cron.list()

    job = CronScheduler(processor=_Queue(), entries=[], cron_store=cron).build_job(
        CronEntry.from_row(row), datetime.now(UTC)
    )

    assert (job.payload["flow"], job.payload["message"], job.payload["label"]) == (
        REMINDER_FLOW,
        "Stand up and stretch.",
        "Stretch",
    )


async def test_at_its_time_the_reminder_flow_puts_it_on_the_dashboard_with_a_record(tmp_path, home) -> None:
    """The run a schedule starts: the reminder flow, its step given the schedule's text."""
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(
        FlowRegistry(builtin_dir=_BUILTIN, learned_dir=tmp_path / "none"),
        FakeAgents(FakeAgent()),
        _skills(tmp_path),
        store,
    )
    runner.register_action("deliver_reminder", lambda inputs: _deliver_reminder(inputs, home["approvals"]))

    out = await RunFlowTool(runner).run_scheduled(
        REMINDER_FLOW, "j1", {"message": "Time to take the laundry out.", "label": "Laundry", "late": ""}
    )

    assert out.success, out.error
    [notice] = home["notices"].waiting()
    assert (notice.title, notice.message, notice.source) == ("Laundry", "Time to take the laundry out.", "reminder")
    [record] = [e for e in await home["ledger"].query(LedgerFilters()) if e.action == "notice_delivered"]
    assert record.agent_output["notice_id"] == notice.id
    assert store.get(out.data["run_id"]).outputs[0]["data"]["summary"] == "Delivered the reminder 'Laundry'."


async def test_a_reminder_that_ran_late_says_so(home) -> None:
    await _deliver_reminder({"message": "Call mom.", "label": "", "late": "ran 5 min late"}, home["approvals"])

    [notice] = home["notices"].waiting()
    assert notice.message == "Call mom.\n\n(ran 5 min late)"


async def test_a_reminder_with_no_text_is_refused(home) -> None:
    with pytest.raises(ValueError, match="text"):
        await _deliver_reminder({"message": " ", "label": "", "late": ""}, home["approvals"])
