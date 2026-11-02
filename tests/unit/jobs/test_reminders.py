"""A reminder is a job carrying its message, delivered at its time, to the dashboard, with a record.

In the issue #76 test each one-off reminder became a permanent flow that ran
once as a "test" when it was approved (finding M17), and the run at its time
reached no one on a dashboard-only install while north had said "It will tell
you" (M4). One that came due while north was off ran later, saying nothing of
it (M18).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.notices import NoticeStore
from approval.store import ApprovalStore
from jobs.cron_store import UserCronStore
from jobs.models import Job, JobType
from jobs.scheduler import CronEntry, CronScheduler
from ledger import SQLiteLedgerWriter
from ledger.base import LedgerFilters
from orchestrator.app import _deliver_reminder
from tests.conftest import approval_policy
from tools.models import ToolInput
from tools.universal.schedule_task import ScheduleTaskTool


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)


@pytest.fixture
def queue() -> _Queue:
    return _Queue()


@pytest.fixture
def cron(tmp_path: Path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


def _soon() -> str:
    return (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")


async def test_a_one_off_reminder_is_a_job_carrying_its_message(queue, cron) -> None:
    tool = ScheduleTaskTool(job_processor=queue, cron_store=cron)

    out = await tool.run(
        ToolInput(params={"message": "Time to take the laundry out.", "label": "Laundry", "run_at": _soon()})
    )

    assert out.success, out.error
    [job] = queue.jobs
    assert job.payload["message"] == "Time to take the laundry out."
    assert job.payload["label"] == "Laundry"
    assert "flow" not in job.payload


async def test_setting_one_up_says_only_where_it_will_appear(queue, cron) -> None:
    dashboard_only = ScheduleTaskTool(job_processor=queue, cron_store=cron)
    out = await dashboard_only.run(ToolInput(params={"message": "Call mom.", "run_at": _soon()}))
    assert out.data["delivered_to"] == ["the dashboard"]
    assert dashboard_only.format_output(out.data).endswith("It will appear on the dashboard.")

    with_telegram = ScheduleTaskTool(
        job_processor=queue, cron_store=cron, reaches=lambda: ["the dashboard", "Telegram"]
    )
    out = await with_telegram.run(ToolInput(params={"message": "Call mom.", "run_at": _soon()}))
    assert with_telegram.format_output(out.data).endswith("It will appear on the dashboard and on Telegram.")


async def test_a_recurring_reminder_carries_its_message_to_every_firing(queue, cron) -> None:
    tool = ScheduleTaskTool(job_processor=queue, cron_store=cron)

    out = await tool.run(
        ToolInput(params={"message": "Stand up and stretch.", "label": "Stretch", "hour": 15, "days": "weekdays"})
    )

    assert out.success, out.error
    [row] = await cron.list()
    job = CronScheduler(processor=queue, entries=[], cron_store=cron).build_job(
        CronEntry.from_row(row), datetime.now(UTC)
    )
    assert (job.payload["message"], job.payload["label"]) == ("Stand up and stretch.", "Stretch")


async def test_a_reminder_and_a_flow_at_once_is_refused(queue, cron) -> None:
    tool = ScheduleTaskTool(job_processor=queue, cron_store=cron)

    out = await tool.run(ToolInput(params={"message": "Call mom.", "flow": "call-mom", "run_at": _soon()}))

    assert not out.success
    assert queue.jobs == []


@pytest.fixture
def home(tmp_path: Path) -> dict:
    """A dashboard-only install: no TUI, Telegram or macOS notifier, and no model."""
    notices = NoticeStore(tmp_path / "notices.db")
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    approvals = Approvals(approval_policy(), UserInteraction(ApprovalStore(), notices=notices, ledger=ledger))
    return {"notices": notices, "ledger": ledger, "approvals": approvals}


def _due(minutes_late: int) -> Job:
    slot = datetime.now(UTC) - timedelta(minutes=minutes_late)
    return Job(
        job_id="j1",
        type=JobType.ASYNC,
        agent="general",
        task="Laundry",
        payload={"scheduled_by": "schedule_task", "message": "Time to take the laundry out.", "label": "Laundry"},
        scheduled_at=slot,
        started_at=datetime.now(UTC),
    )


async def test_at_its_time_the_reminder_waits_on_the_dashboard_with_a_record(home) -> None:
    await _deliver_reminder(_due(0), "Time to take the laundry out.", home["approvals"])

    [notice] = home["notices"].waiting()
    assert (notice.title, notice.message, notice.source) == ("Laundry", "Time to take the laundry out.", "reminder")
    [record] = [e for e in await home["ledger"].query(LedgerFilters()) if e.action == "notice_delivered"]
    assert record.agent_output["notice_id"] == notice.id


async def test_a_reminder_that_ran_late_says_so(home) -> None:
    await _deliver_reminder(_due(5), "Call mom.", home["approvals"])

    [notice] = home["notices"].waiting()
    assert notice.message.startswith("Call mom.\n\n(ran ")
    assert "5 min late" in notice.message
