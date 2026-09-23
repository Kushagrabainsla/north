from __future__ import annotations

from orchestrator.api_context import ApiServices, bind_services
from tools.registry import ToolRegistry
from tools.universal.create_tool import _read_tool
from web import api as web_api


async def test_dashboard_tool_edits_stay_candidates_until_runtime_activation(tmp_path) -> None:
    learned = tmp_path / "learned" / "tools"
    registry = ToolRegistry(learned_dir=learned)

    with bind_services(ApiServices(tool_registry=registry, north_home=tmp_path)):
        created = await web_api.create_tool(
            web_api.ToolCreate(
                name="job_lookup",
                description="Look up one job listing.",
                tool_type="specialized",
            )
        )
        detail = await web_api.get_tool("job_lookup")
        listed = await web_api.list_tools()

    candidate = learned / "candidates" / "specialized" / "job_lookup.py"
    active = learned / "specialized" / "job_lookup.py"
    assert created["status"] == "candidate"
    assert detail["status"] == "candidate"
    assert next(row for row in listed if row["name"] == "job_lookup")["status"] == "candidate"
    assert candidate.exists()
    assert not active.exists()
    assert "job_lookup" not in registry.all_tool_names()


async def test_dashboard_edit_of_builtin_tool_creates_candidate_override(tmp_path) -> None:
    learned = tmp_path / "learned" / "tools"
    registry = ToolRegistry(auto_register=True, learned_dir=learned)
    original = registry.get("read_file")
    content = _read_tool("read_file", learned).data["content"]

    with bind_services(ApiServices(tool_registry=registry, north_home=tmp_path)):
        result = await web_api.update_tool("read_file", web_api.ToolUpdate(content=content))

    assert result["status"] == "candidate"
    assert learned.joinpath("candidates", "universal", "read_file.py").exists()
    assert registry.get("read_file") is original
