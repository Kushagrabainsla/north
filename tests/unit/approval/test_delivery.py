"""What north delivers reaches the dashboard, on every install, and is recorded.

A scheduled run's result and a reminder went only to the system notifiers (the
TUI, Telegram, macOS). On a dashboard-only install there are none: in the issue
#76 test (finding M4) a reminder fired on time, its step said
`"reminder_sent": true`, and nothing appeared in the session, the Attention
panel, Artifacts or Approvals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from approval.interaction import UserInteraction
from approval.notices import NoticeStore
from approval.store import ApprovalStore
from flows import FlowRunStore
from ledger import SQLiteLedgerWriter
from ledger.base import LedgerFilters
from orchestrator.api_context import ApiServices, bind_services
from tests.unit.flows.test_runner import _ONE_STEP, BriefingAgent, FakeAgents, _flow, _skills, _users_flow
from tools.universal.flow_runner import FlowRunner
from web import api as web_api


@pytest.fixture
def home(tmp_path: Path) -> dict:
    """A dashboard-only install: no TUI, Telegram or macOS notifier."""
    notices = NoticeStore(tmp_path / "notices.db")
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    interaction = UserInteraction(ApprovalStore(), notices=notices, ledger=ledger)
    return {"notices": notices, "ledger": ledger, "interaction": interaction, "path": tmp_path}


async def test_a_scheduled_runs_result_waits_on_the_dashboard(home) -> None:
    tmp_path = home["path"]
    runner = FlowRunner(
        _users_flow(tmp_path, _ONE_STEP),
        FakeAgents(BriefingAgent(tmp_path)),
        _skills(tmp_path),
        FlowRunStore(tmp_path / "runs.db"),
        home["interaction"],
    )

    await runner.run("demo", trigger="schedule")

    with bind_services(ApiServices(notice_store=home["notices"], north_home=tmp_path)):
        [notice] = await web_api.notices()
    assert notice["title"] == "demo is ready"
    assert "PWM: Personalized World Models" in notice["message"]


async def test_north_finishing_its_own_housekeeping_is_not_delivered(home) -> None:
    """Delivered, "nightly-cleanup is ready: removed 0 stale rows" arrived every night and buried the reminders."""
    tmp_path = home["path"]
    runner = FlowRunner(
        _flow(tmp_path, _ONE_STEP),
        FakeAgents(BriefingAgent(tmp_path)),
        _skills(tmp_path),
        FlowRunStore(tmp_path / "runs.db"),
        home["interaction"],
    )

    run = await runner.run("demo", trigger="schedule")

    assert run.status == "completed"
    assert home["notices"].waiting() == []


async def test_a_delivery_is_recorded_and_outlives_a_restart(home) -> None:
    notice = await home["interaction"].deliver(title="Reminder", message="Take the laundry out.", source="reminder")

    [record] = [e for e in await home["ledger"].query(LedgerFilters()) if e.action == "notice_delivered"]
    assert record.output == "Take the laundry out."
    assert record.agent_output == {"notice_id": notice.id, "dashboard": True}
    assert [n.id for n in NoticeStore(home["path"] / "notices.db").waiting()] == [notice.id]


async def test_a_dismissed_notice_leaves_the_dashboard(home) -> None:
    notice = await home["interaction"].deliver(title="Reminder", message="Call mom.", source="reminder")

    with bind_services(ApiServices(notice_store=home["notices"], north_home=home["path"])):
        await web_api.dismiss_notice(notice.id)
        assert await web_api.notices() == []
        with pytest.raises(web_api.HTTPException):
            await web_api.dismiss_notice(notice.id)


async def test_a_passing_notice_is_not_kept() -> None:
    """A chat task's "Done" ping is for whoever is watching; keeping it would bury the reminders."""
    notices = NoticeStore()
    await UserInteraction(ApprovalStore(), notices=notices).inform(
        task_id="t1", agent="general", title="General - Done", message="Answered."
    )

    assert notices.waiting() == []
