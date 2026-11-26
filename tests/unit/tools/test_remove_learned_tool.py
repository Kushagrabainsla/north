"""A tool north wrote can be removed while north runs, and nothing of it is left.

Before: the registry only ever added learned tools, so a tool whose file was deleted stayed callable, and
`create_tool` refused to make it again ("already exists") until north restarted. The dashboard's delete
removed only the code: the tool's process, environment and own folder stayed. `create_tool` had no remove.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.api_context import ApiServices, bind_services
from tools.base import Tool
from tools.learned_remote import RunnerTool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from tools.universal.create_tool import CreateToolTool
from web import extensions

TOOL = """
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class Lamps(Tool):
    name = "lamps"
    description = "Lists lamps."
    parameters_schema = {"type": "object", "properties": {"action": {"type": "string"}}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={"lamps": ["desk"]})
"""
CREATE = {"action": "create", "name": "lamps", "content": TOOL, "test_params": {"action": "list"}}


class Clock(Tool):
    name = "clock"
    description = "Built in."
    parameters_schema = {"type": "object", "properties": {}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={})


@pytest.fixture
def learned(tmp_path: Path) -> Path:
    return tmp_path / "learned" / "tools"


@pytest.fixture
def registry(learned: Path) -> ToolRegistry:
    learned.mkdir(parents=True)
    return ToolRegistry(auto_register=True, learned_dir=learned)


@pytest.fixture
def creator(registry: ToolRegistry, learned: Path) -> CreateToolTool:
    tool = CreateToolTool(tool_registry=registry, tools_dir=learned, runtime=registry.learned_runtime())
    registry.register(tool)
    return tool


async def _made(creator: CreateToolTool, registry: ToolRegistry, learned: Path) -> RunnerTool:
    out = await creator.run(ToolInput(params=CREATE))
    assert out.success, out.error
    tool = registry.get("lamps")
    assert isinstance(tool, RunnerTool)
    assert (await tool.run(ToolInput(params={"action": "list"}))).success  # its process is running
    # What a tool with packages has besides its code: its environment and its own folder.
    (learned.parent / "envs" / "lamps-0123abcd").mkdir(parents=True)
    (learned.parent / "data" / "lamps" / "state.json").write_text("{}", encoding="utf-8")
    return tool


def _left(learned: Path) -> list[str]:
    root = learned.parent
    return sorted(str(p.relative_to(root)) for p in root.rglob("*lamps*"))


async def test_create_tool_removes_a_tool_north_wrote_and_nothing_of_it_is_left(creator, registry, learned) -> None:
    tool = await _made(creator, registry, learned)

    out = await creator.run(ToolInput(params={"action": "remove", "name": "lamps"}))

    assert out.success, out.error
    assert "lamps" not in registry.all_tool_names()
    assert _left(learned) == [], "code, what the runner said, environment and own folder are all gone"
    assert tool._client is None, "its process is stopped"


async def test_the_same_tool_can_be_made_again_at_once_with_no_restart(creator, registry, learned) -> None:
    await _made(creator, registry, learned)
    assert (await creator.run(ToolInput(params={"action": "remove", "name": "lamps"}))).success

    again = await creator.run(ToolInput(params=CREATE))

    assert again.success, again.error
    assert isinstance(registry.get("lamps"), RunnerTool)
    await registry.get("lamps").aclose()


async def test_a_tool_whose_file_is_deleted_is_dropped_on_the_next_scan(creator, registry, learned) -> None:
    tool = await _made(creator, registry, learned)

    (learned / "specialized" / "lamps.py").unlink()
    (learned / "specialized" / "lamps.json").unlink()
    registry.reload()

    assert "lamps" not in registry.all_tool_names()
    out = await creator.run(ToolInput(params=CREATE))
    assert out.success, f"no 'already exists' for a tool that is gone: {out.error}"
    await registry.get("lamps").aclose()
    assert tool is not registry.get("lamps")


async def test_a_draft_that_never_went_live_is_removed_too(creator, registry, learned) -> None:
    draft = learned / "candidates" / "specialized" / "lamps.py"
    draft.parent.mkdir(parents=True)
    draft.write_text(TOOL, encoding="utf-8")

    out = await creator.run(ToolInput(params={"action": "remove", "name": "lamps"}))

    assert out.success, out.error
    assert not draft.exists()


async def test_a_built_in_tool_is_never_removed(creator, registry) -> None:
    registry.register(Clock())

    out = await creator.run(ToolInput(params={"action": "remove", "name": "clock"}))

    assert not out.success and "built in" in out.error
    assert "clock" in registry.all_tool_names()
    missing = await creator.run(ToolInput(params={"action": "remove", "name": "nothing_here"}))
    assert not missing.success and "action='list'" in missing.error


async def test_removing_asks_first_and_says_what_goes(creator) -> None:
    assert creator.mutates({"action": "remove", "name": "lamps"})

    card = await creator.describe(ToolInput(params={"action": "remove", "name": "lamps"}))

    assert card.action.operation == "remove" and card.action.args == "lamps"
    assert "environment" in card.message and "cannot be undone" in card.message


async def test_the_dashboard_delete_removes_the_process_environment_and_folder(
    creator, registry, learned, tmp_path
) -> None:
    tool = await _made(creator, registry, learned)

    with bind_services(ApiServices(tool_registry=registry, north_home=tmp_path)):
        await extensions.delete_tool("lamps")

    assert "lamps" not in registry.all_tool_names()
    assert _left(learned) == []
    assert tool._client is None
