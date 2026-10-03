"""Question: is every kind of wait visible on north's dashboard and does it leave the queue free?"""

from __future__ import annotations

from agents.models import AgentPayload
from approval.models import Card, CardType
from approval.store import ApprovalStore
from jobs.models import Job, JobType
from orchestrator.agent_runs import AgentRunStore
from orchestrator.api_context import bind_services, services_of
from orchestrator.constants import MAX_CONCURRENT_TASKS
from orchestrator.orchestrator import Orchestrator
from utils.time import utcnow

from .prototypes import record


def _card(task_id: str | None) -> Card:
    return Card.new(type=CardType.APPROVAL, task_id=task_id, agent="coding:claude", title="Shell", message="make build")


def _orchestrator(store: ApprovalStore, busy: int) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)  # one method, not the whole graph
    orchestrator._approval_store = store
    orchestrator._active_tasks = {f"t{i}": object() for i in range(busy)}
    return orchestrator


def test_a_gate_card_frees_the_slot_only_when_it_names_the_task() -> None:
    store = ApprovalStore()
    orchestrator = _orchestrator(store, MAX_CONCURRENT_TASKS)
    store.add(_card("t0"))
    store.add(_card(None))  # a gate that forgot the task id

    assert orchestrator._working_tasks() == MAX_CONCURRENT_TASKS - 1  # t0 freed; the None card freed nothing
    record("S1_slots", {"cap": MAX_CONCURRENT_TASKS, "working_with_named_card": orchestrator._working_tasks()})


async def test_dashboard_attention_lists_a_pending_gate_card(booted_app) -> None:
    from web import api as web_api

    services = services_of(booted_app)
    services.approval_store.add(_card("t-run"))

    with bind_services(services):
        payload = await web_api.dashboard()

    assert [item["agent"] for item in payload["attention"]] == ["coding:claude"]
    record("S2_dashboard_attention", {"fields": sorted(payload["attention"][0])})


async def test_a_coding_run_is_readable_through_the_runs_api(booted_app) -> None:
    from orchestrator.api import runs as runs_api

    services = services_of(booted_app)
    store: AgentRunStore = services.agent_run_store
    payload = AgentPayload(task_id="t-run", prompt="fix the bug", workspace="/work/repo-wt", delegation_depth=1)
    await store.start(payload, "coding:claude")
    await store.merge_provider_state(
        payload.run_id, {"provider": "claude_code", "session_id": "uuid-1", "worktree": "/work/repo-wt", "pid": 123}
    )
    await store.record_event(payload.run_id, "t-run", "waiting_for_approval", {"summary": "make build"})

    with bind_services(services):
        listed = await runs_api.task_agent_runs("t-run")
        events = await runs_api.agent_run_events(payload.run_id)

    assert listed[0].agent == "coding:claude"
    assert listed[0].provider_state["claude_code"][-1]["session_id"] == "uuid-1"  # keyed by provider, append-only
    assert [event["event"] for event in events] == ["waiting_for_approval"]
    record("S3_run", {"status": listed[0].status, "provider_state_keys": sorted(listed[0].provider_state)})


async def test_a_job_parked_for_attention_shows_on_the_dashboard(booted_app) -> None:
    from web import api as web_api

    services = services_of(booted_app)
    job = Job(job_id="j1", type=JobType.ASYNC, agent="coding", task="fix bug", scheduled_at=utcnow())
    await services.job_processor.enqueue(job)
    await services.job_processor.mark_needs_attention("j1")

    with bind_services(services):
        payload = await web_api.dashboard()

    assert {entry["job_id"]: entry["status"] for entry in payload["jobs"]}["j1"] == "needs_attention"
