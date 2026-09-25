"""Managing a flow from the dashboard: schedule it, run it, test it, activate it.

These routes are thin over the tools the chat uses, so the tests check the
part that is the page's own: what it refuses, what it returns, and that a run
is visible the moment it starts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from agents.models import AgentResult
from flows.models import flow_fingerprint
from flows.registry import FlowRegistry
from flows.store import FlowRunStore
from jobs.cron_store import UserCronStore
from orchestrator.api_context import ApiServices, bind_services
from skills.registry import SkillRegistry
from tools.universal.flow_runner import FlowRunner
from utils.tasks import drain
from web import api as web_api

SKILL = """---
name: review-item
description: "Use when reviewing one item."
domains: [general]
execution:
  agent: general
  tools: []
  approval: never
  inputs:
    type: object
    properties: {}
    additionalProperties: true
  outputs:
    type: object
    properties: {}
    additionalProperties: true
  success_criteria:
    - A structured review result was returned.
---
# Review item

Inspect the supplied item and return a structured result.
"""


def _flow_yaml(name: str, status: str) -> str:
    return yaml.safe_dump(
        {
            "name": name,
            "description": f"Review {name}",
            "status": status,
            "domains": ["general"],
            "steps": [{"name": "look", "skill": "review-item", "instructions": "", "approval": "never"}],
        },
        sort_keys=False,
    )


class FakeAgent:
    name = "general"
    domain = "general"
    config = SimpleNamespace(model_pool="reasoning", produces=[])

    async def run(self, payload):
        return AgentResult(output="{}", summary="looked", data={}, tools_used=[])


class FakeAgents:
    def __init__(self) -> None:
        self.agent = FakeAgent()

    def get(self, name: str):
        if name != "general":
            raise KeyError(name)
        return self.agent

    def names(self) -> list[str]:
        return ["general"]


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list = []

    async def enqueue(self, job) -> None:
        self.enqueued.append(job)

    async def get(self, job_id):
        return next((job for job in self.enqueued if job.job_id == job_id), None)

    async def cancel(self, job_id) -> None:
        self.enqueued = [job for job in self.enqueued if job.job_id != job_id]


class Env:
    def __init__(self, tmp_path) -> None:
        skills_dir = tmp_path / "skills" / "review-item"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(SKILL, encoding="utf-8")
        self.skills = SkillRegistry(tmp_path / "skills")

        self.builtin_dir = tmp_path / "builtin"
        self.learned_dir = tmp_path / "flows"
        self._add(self.learned_dir, "draft", "candidate")
        self._add(self.learned_dir, "live", "active")
        self._add(self.builtin_dir, "shipped", "active")
        self.flows = FlowRegistry(self.builtin_dir, self.learned_dir)
        self.activate_live()

        self.agents = FakeAgents()
        self.store = FlowRunStore(tmp_path / "runs.db")
        self.cron = UserCronStore(tmp_path / "jobs.db")
        self.jobs = FakeJobs()
        self.runner = FlowRunner(self.flows, self.agents, self.skills, self.store)
        self.services = ApiServices(
            flow_registry=self.flows,
            skill_registry=self.skills,
            agent_registry=self.agents,
            flow_store=self.store,
            flow_runner=self.runner,
            cron_store=self.cron,
            job_processor=self.jobs,
            north_home=tmp_path,
        )

    @staticmethod
    def _add(base, name: str, status: str) -> None:
        directory = base / name
        directory.mkdir(parents=True)
        (directory / "FLOW.yaml").write_text(_flow_yaml(name, status), encoding="utf-8")

    def activate_live(self) -> None:
        for name in ("live", "shipped"):
            flow = self.flows.get(name)
            data = yaml.safe_load((flow.directory / "FLOW.yaml").read_text(encoding="utf-8"))
            data["activation_fingerprint"] = flow_fingerprint(flow, self.skills.get)
            (flow.directory / "FLOW.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.flows.reload()


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    with bind_services(environment.services):
        yield environment


async def test_an_active_flow_can_be_put_on_a_daily_schedule(env) -> None:
    created = await web_api.create_flow_schedule(
        "live", web_api.FlowScheduleCreate(label="Morning look", hour=7, minute=30, days="weekdays")
    )

    assert created["flow"] == "live"
    assert created["cadence"] == "weekdays"
    row = await env.cron.get(created["name"])
    assert row["flow"] == "live" and row["hour"] == 7 and row["minute"] == 30


async def test_a_candidate_cannot_be_scheduled(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.create_flow_schedule("draft", web_api.FlowScheduleCreate(hour=7))

    assert refused.value.status_code == 422
    assert "not active" in refused.value.detail


async def test_a_schedule_needs_exactly_one_kind_of_timing(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.create_flow_schedule("live", web_api.FlowScheduleCreate())

    assert refused.value.status_code == 422


async def test_an_unknown_flow_is_a_404(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.create_flow_schedule("nope", web_api.FlowScheduleCreate(hour=7))

    assert refused.value.status_code == 404


async def test_a_single_run_at_a_set_time_is_queued_as_a_job_for_the_flow(env) -> None:
    created = await web_api.create_flow_schedule(
        "live", web_api.FlowScheduleCreate(run_at="2099-01-01T09:00")
    )

    assert created["type"] == "one-shot"
    assert env.jobs.enqueued[0].payload["flow"] == "live"


async def test_a_flows_schedule_can_be_paused_retimed_and_deleted(env) -> None:
    created = await web_api.create_flow_schedule("live", web_api.FlowScheduleCreate(hour=7))
    name = created["name"]

    paused = await web_api.update_flow_schedule(name, web_api.FlowScheduleUpdate(enabled=False))
    assert paused["enabled"] is False

    retimed = await web_api.update_flow_schedule(name, web_api.FlowScheduleUpdate(hour=9, minute=15))
    assert (retimed["hour"], retimed["minute"]) == (9, 15)

    assert await web_api.delete_flow_schedule(name) is None
    assert await env.cron.get(name) is None


async def test_only_schedules_that_run_a_flow_are_managed_here(env) -> None:
    await env.cron.add(name="stretch", agent="general", task="stretch", hour=9, minute=0, weekdays=None)

    with pytest.raises(HTTPException) as missing:
        await web_api.update_flow_schedule("stretch", web_api.FlowScheduleUpdate(enabled=False))
    with pytest.raises(HTTPException) as also_missing:
        await web_api.delete_flow_schedule("stretch")

    assert missing.value.status_code == 404
    assert also_missing.value.status_code == 404
    assert await env.cron.get("stretch") is not None


async def test_a_test_run_is_visible_at_once_and_finishes_in_the_background(env) -> None:
    started = await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="test"))

    assert started["trigger"] == "test"
    assert env.store.get(started["run_id"]) is not None
    await drain()

    run = env.store.get(started["run_id"])
    assert run.status == "completed"
    assert run.test_mode and run.trigger == "test"
    assert run.task_id == started["task_id"]


async def test_a_real_run_of_a_candidate_is_refused(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="execute"))

    assert refused.value.status_code == 422
    assert "not active" in refused.value.detail
    assert env.store.list_runs() == []


async def test_an_active_flow_can_be_run_by_hand(env) -> None:
    started = await web_api.start_flow_run("live", web_api.FlowRunRequest(mode="execute"))
    await drain()

    run = env.store.get(started["run_id"])
    assert run.status == "completed" and run.trigger == "manual" and not run.test_mode


async def test_a_run_that_blows_up_is_recorded_as_failed_not_left_running(env, monkeypatch) -> None:
    async def explode(*args, **kwargs):
        raise RuntimeError("provider fell over")

    monkeypatch.setattr(env.runner, "run", explode)

    started = await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="test"))
    await drain()

    run = env.store.get(started["run_id"])
    assert run.status == "failed"
    assert "provider fell over" in run.error


async def test_a_candidate_is_offered_activation_only_after_a_passing_test(env) -> None:
    assert (await web_api.get_flow("draft"))["tested_run_id"] == ""

    started = await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="test"))
    await drain()

    assert (await web_api.get_flow("draft"))["tested_run_id"] == started["run_id"]


async def test_a_tested_candidate_can_be_activated_and_then_scheduled(env) -> None:
    started = await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="test"))
    await drain()

    activated = await web_api.activate_flow("draft", web_api.FlowActivation(test_run_id=started["run_id"]))

    assert activated["status"] == "active"
    scheduled = await web_api.create_flow_schedule("draft", web_api.FlowScheduleCreate(hour=8))
    assert scheduled["flow"] == "draft"


async def test_activation_without_a_matching_test_is_refused(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.activate_flow("draft", web_api.FlowActivation(test_run_id="not-a-run"))

    assert refused.value.status_code == 422
    assert env.flows.get("draft").status == "candidate"


async def test_a_built_in_flow_needs_no_activation(env) -> None:
    with pytest.raises(HTTPException) as refused:
        await web_api.activate_flow("shipped", web_api.FlowActivation(test_run_id="x"))

    assert refused.value.status_code == 409


async def test_a_flow_whose_skill_changed_after_activation_cannot_be_scheduled_or_run(env) -> None:
    skill_file = env.skills.get("review-item")
    path = skill_file.directory / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nAlso check the footer.\n", encoding="utf-8")
    env.skills.reload()

    with pytest.raises(HTTPException) as scheduling:
        await web_api.create_flow_schedule("live", web_api.FlowScheduleCreate(hour=7))
    with pytest.raises(HTTPException) as running:
        await web_api.start_flow_run("live", web_api.FlowRunRequest(mode="execute"))

    assert "changed after activation" in scheduling.value.detail
    assert running.value.status_code == 422


def test_the_management_routes_are_mounted_where_the_page_calls_them() -> None:
    routes = {(route.path, method) for route in web_api.router.routes for method in getattr(route, "methods", ())}

    assert ("/web/api/flow-definitions/{name}/schedules", "POST") in routes
    assert ("/web/api/flow-schedules/{schedule}", "PATCH") in routes
    assert ("/web/api/flow-schedules/{schedule}", "DELETE") in routes
    assert ("/web/api/flow-definitions/{name}/runs", "POST") in routes
    assert ("/web/api/flow-definitions/{name}/activate", "POST") in routes
    assert ("/web/api/flow-runs", "GET") in routes


async def test_activation_from_the_page_does_not_go_through_the_self_edit_guarded_tool(env) -> None:
    """The registered create_flow refuses files north did not write itself, and a
    flow made from the page is exactly that. Activating one must still work."""

    class GuardedRegistry:
        def get(self, name: str):
            raise AssertionError(f"the page must not use the registered {name!r} tool")

    started = await web_api.start_flow_run("draft", web_api.FlowRunRequest(mode="test"))
    await drain()

    services = env.services.replace(tool_registry=GuardedRegistry())
    with bind_services(services):
        activated = await web_api.activate_flow("draft", web_api.FlowActivation(test_run_id=started["run_id"]))

    assert activated["status"] == "active"
