from __future__ import annotations

import pytest
from fastapi import HTTPException

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
        + "\nversion: 1.0.0\ndomains: [general]\nstatus: active\nsteps:\n"
        + "  - name: inspect\n    tool: browser\n    approval: always\n"
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
    assert result["content"] == updated
    assert flow_registry.get("job-review").description == "Updated job review"


async def test_flow_api_creates_and_deletes_learned_flow(tmp_path) -> None:
    registry = FlowRegistry(tmp_path / "builtin", tmp_path / "flows")
    with bind_services(ApiServices(flow_registry=registry, north_home=tmp_path)):
        created = await web_api.create_flow(web_api.FlowCreate(content=_document("new-flow")))
        assert created["name"] == "new-flow"
        assert registry.get("new-flow").source is FlowSource.LEARNED
        assert await web_api.delete_flow("new-flow") is None
        with pytest.raises(FlowNotFoundError):
            registry.get("new-flow")


async def test_flow_api_rejects_builtin_edits(tmp_path) -> None:
    builtin = tmp_path / "builtin" / "system-flow"
    builtin.mkdir(parents=True)
    (builtin / "FLOW.yaml").write_text(_document("system-flow"), encoding="utf-8")
    registry = FlowRegistry(tmp_path / "builtin", tmp_path / "learned")
    with bind_services(ApiServices(flow_registry=registry, north_home=tmp_path)):
        with pytest.raises(HTTPException) as exc:
            await web_api.update_flow("system-flow", web_api.FlowUpdate(content=_document("system-flow", "changed")))
        assert exc.value.status_code == 403
