from __future__ import annotations

import pytest

from flows.exceptions import FlowNotFoundError
from flows.models import FlowSource
from flows.registry import FlowRegistry
from orchestrator.api_context import ApiServices, bind_services
from web import api as web_api


def _document(name: str = "job-review", description: str = "Review a prepared job") -> str:
    return (
        "name: "
        + name
        + "\n"
        + "description: "
        + description
        + "\ndomains: [general]\nstatus: active\nsteps:\n"
        + "  - name: inspect\n"
        + "    skill: review-item\n"
        + "    instructions: Review the prepared job.\n"
        + "    approval: always\n"
    )


@pytest.fixture
def flow_registry(tmp_path) -> FlowRegistry:
    learned = tmp_path / "learned"
    flow_dir = learned / "job-review"
    flow_dir.mkdir(parents=True)
    (flow_dir / "FLOW.yaml").write_text(_document(), encoding="utf-8")
    registry = FlowRegistry(tmp_path / "builtin", learned)
    with bind_services(ApiServices(flow_registry=registry, north_home=tmp_path)):
        yield registry


def test_flow_routes_use_non_conflicting_dashboard_paths() -> None:
    paths = {route.path for route in web_api.router.routes}
    assert "/web/api/flow-definitions" in paths
    assert "/web/api/flow-definitions/{name}" in paths
    assert "/web/api/flows/{name}" not in paths


async def test_flow_api_lists_reads_and_updates(flow_registry: FlowRegistry) -> None:
    listed = await web_api.list_flows()
    assert listed[0]["name"] == "job-review"
    detail = await web_api.get_flow("job-review")
    assert "approval: always" in detail["content"]

    updated = _document(description="Updated job review")
    result = await web_api.update_flow("job-review", web_api.FlowUpdate(content=updated))
    assert result["status"] == "candidate"
    assert "version:" not in result["content"]
    assert flow_registry.get("job-review").description == "Updated job review"
    assert flow_registry.get("job-review").status == "candidate"


async def test_flow_api_creates_and_deletes_learned_flow(tmp_path) -> None:
    registry = FlowRegistry(tmp_path / "builtin", tmp_path / "flows")
    with bind_services(ApiServices(flow_registry=registry, north_home=tmp_path)):
        created = await web_api.create_flow(web_api.FlowCreate(content=_document("new-flow")))
        assert created["name"] == "new-flow"
        assert created["status"] == "candidate"
        assert registry.get("new-flow").source is FlowSource.LEARNED
        assert await web_api.delete_flow("new-flow") is None
        with pytest.raises(FlowNotFoundError):
            registry.get("new-flow")


async def test_flow_api_creates_user_override_for_builtin_edits(tmp_path) -> None:
    builtin = tmp_path / "builtin" / "system-flow"
    builtin.mkdir(parents=True)
    (builtin / "FLOW.yaml").write_text(_document("system-flow"), encoding="utf-8")
    registry = FlowRegistry(tmp_path / "builtin", tmp_path / "flows")
    with bind_services(ApiServices(flow_registry=registry, north_home=tmp_path)):
        result = await web_api.update_flow(
            "system-flow",
            web_api.FlowUpdate(content=_document("system-flow", "changed")),
        )

    assert result["source"] == "learned"
    assert registry.get("system-flow").description == "changed"
    assert registry.get("system-flow").source is FlowSource.LEARNED


def _run_store(tmp_path):
    from flows.store import FlowRunStore

    return FlowRunStore(tmp_path / "runs.db")


async def test_flow_runs_list_newest_first_with_trigger_and_progress(flow_registry, tmp_path) -> None:
    store = _run_store(tmp_path)
    store.create(run_id="first", flow_name="job-review", trigger="schedule")
    store.create(run_id="second", flow_name="other", trigger="manual")
    store.update(
        "first",
        status="completed",
        current_step=1,
        outputs=[
            {
                "step": "inspect",
                "skill": "review-item",
                "agent": "general",
                "data": {"summary": "looked at it", "output": "ok", "tools_used": ["web_search"]},
            }
        ],
    )

    services = ApiServices(flow_registry=flow_registry, flow_store=store, north_home=tmp_path)
    with bind_services(services):
        every = await web_api.list_flow_runs()
        one = await web_api.list_flow_runs(flow="job-review")

    assert [run["run_id"] for run in every] == ["second", "first"]
    assert [run["run_id"] for run in one] == ["first"]
    finished = one[0]
    assert finished["trigger"] == "schedule"
    assert finished["status"] == "completed"
    assert finished["total_steps"] == 1
    assert finished["steps"][0]["summary"] == "looked at it"
    assert finished["steps"][0]["tools_used"] == ["web_search"]
    # A flow that no longer exists still has readable history.
    assert every[0]["total_steps"] == 0


def test_run_view_clips_long_output_and_names_old_test_runs(tmp_path) -> None:
    from flows.store import FlowRunStore
    from web.extensions import flow_run_view

    store = FlowRunStore(tmp_path / "runs.db")
    store.create(run_id="r", flow_name="demo", test_mode=True)
    run = store.update(
        "r",
        status="completed",
        current_step=1,
        outputs=[{"step": "s", "skill": "", "agent": "general", "data": {"output": "x" * 5000}}],
    )
    # Runs from before triggers were recorded carry no trigger of their own.
    legacy = run.__class__(**{**run.__dict__, "trigger": ""})

    view = flow_run_view(legacy, total_steps=1)

    assert view["trigger"] == "test"
    assert len(view["steps"][0]["output"]) <= 1_201
    assert view["steps"][0]["output"].endswith("…")


def test_run_view_lists_the_files_a_step_wrote_and_drops_an_empty_answer(tmp_path) -> None:
    from flows.store import FlowRunStore
    from web.extensions import flow_run_view

    store = FlowRunStore(tmp_path / "runs.db")
    store.create(run_id="r", flow_name="demo", trigger="schedule")
    run = store.update(
        "r",
        status="completed",
        current_step=1,
        outputs=[
            {
                "step": "s",
                "skill": "news-briefing",
                "agent": "news_briefing",
                "data": {
                    "summary": "{}",
                    "output": "{}",
                    "artifacts": ["/home/u/.north/tasks/job1/news/2026-09-25.md"],
                },
            }
        ],
    )

    step = flow_run_view(run, total_steps=1)["steps"][0]

    assert step["output"] == "" and step["summary"] == ""
    assert step["artifacts"] == [{"name": "2026-09-25.md", "kind": "news"}]


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, name: str, **kwargs):
        self.calls.append((name, kwargs))


def _paused_run(store, flow_registry, *, fingerprint: str | None = None):
    from flows.models import flow_fingerprint

    run = store.create(
        run_id="r1",
        flow_name="job-review",
        task_id="flow_r1",
        flow_fingerprint=fingerprint if fingerprint is not None else flow_fingerprint(flow_registry.get("job-review")),
        trigger="schedule",
    )
    store.update(run.run_id, status="paused", current_step=0, outputs=[], error="model down")
    return run


async def test_a_paused_run_resumes_as_itself_at_its_step(flow_registry, tmp_path) -> None:
    """#33: Resume runs the same run id, so finished steps and its task's record carry over."""
    import asyncio

    from web import extensions

    store, runner = _run_store(tmp_path), _RecordingRunner()
    _paused_run(store, flow_registry)
    with bind_services(ApiServices(flow_registry=flow_registry, flow_store=store, flow_runner=runner)):
        out = await extensions.resume_flow_run("r1")
        await asyncio.sleep(0)

    assert out["resumed_at_step"] == 0
    assert runner.calls == [
        ("job-review", {"run_id": "r1", "task_id": "flow_r1", "test_mode": False, "trigger": "schedule"})
    ]


async def test_only_a_paused_unchanged_run_can_resume(flow_registry, tmp_path) -> None:
    from fastapi import HTTPException

    from web import extensions

    store, runner = _run_store(tmp_path), _RecordingRunner()
    _paused_run(store, flow_registry, fingerprint="an-older-version")
    store.create(run_id="done", flow_name="job-review")
    with bind_services(ApiServices(flow_registry=flow_registry, flow_store=store, flow_runner=runner)):
        with pytest.raises(HTTPException) as changed:
            await extensions.resume_flow_run("r1")
        with pytest.raises(HTTPException) as running:
            await extensions.resume_flow_run("done")
        with pytest.raises(HTTPException) as missing:
            await extensions.resume_flow_run("nope")

    assert changed.value.status_code == 409 and "changed since this run started" in changed.value.detail
    assert running.value.status_code == 409
    assert missing.value.status_code == 404
    assert runner.calls == []


async def test_a_paused_run_can_be_discarded(flow_registry, tmp_path) -> None:
    from web import extensions

    store = _run_store(tmp_path)
    _paused_run(store, flow_registry)
    with bind_services(ApiServices(flow_registry=flow_registry, flow_store=store)):
        out = await extensions.discard_flow_run("r1")

    assert out["status"] == "cancelled"
