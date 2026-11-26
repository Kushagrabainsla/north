"""The adding-a-home-device guide's example is a tool north can make and a panel north can add, as written.

North rebuilds a brand's devices (Kasa, Hue) from one prompt by following this guide. If its example did
not pass create_tool's checks, or its panel did not pass create_view's, every model that copied it would
fail the same way.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from tools.learned_remote import LearnedRuntime, RunnerTool, describe_file
from tools.registry import ToolRegistry
from tools.universal._draft_checks import draft_problems
from utils.runtime_resources import builtin_skills_dir
from views import check_view
from views.document import action_tool_check, reference_checks

GUIDE = (builtin_skills_dir() / "adding-a-home-device" / "SKILL.md").read_text(encoding="utf-8")
TOOL = re.search(r"```python\n(.*?)```", GUIDE, re.S).group(1)
PANEL = json.loads(re.search(r"```json\n(.*?)```", GUIDE, re.S).group(1))["view"]


def test_the_example_tool_passes_the_draft_checks() -> None:
    assert draft_problems(TOOL, {"hue_bridge"}, own_environment=True) == []


async def test_the_runner_loads_it_and_its_list_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "hue_lights.py"
    path.write_text(TOOL, encoding="utf-8")

    meta = await describe_file(path, "hue_lights", [], LearnedRuntime(tmp_path, sandbox=None))

    assert meta["name"] == "hue_lights" and meta["uses_secrets"] == ["hue_bridge"]
    assert meta["read_only_when"] == {"action": ["list"]}


def test_the_example_panel_passes_the_view_checks_against_that_tool(tmp_path: Path) -> None:
    meta = {"name": "hue_lights", "is_mutating": True, "read_only_when": {"action": ["list"]}}
    registry = ToolRegistry(auto_register=False)
    registry.register(RunnerTool(tmp_path / "hue_lights.py", meta, [], LearnedRuntime(tmp_path, sandbox=object())))
    tool_problem, _ = reference_checks(registry, None)

    assert check_view(PANEL, tool_problem=tool_problem, action_tool_problem=action_tool_check(registry)) == []
    assert {"Switch", "Slider", "ColorPicker", "Microphone"} <= {part["component"] for part in PANEL["components"]}


def test_the_example_panel_has_a_control_for_every_setting_the_tool_can_set() -> None:
    # North copies this example. In the one-prompt Kasa rebuild the example had no white control,
    # so north's panel could turn a light green but never white again.
    schema = next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(TOOL))
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "parameters_schema"
    )
    # Saturation goes with hue, as the guide says: one colour control sends both.
    settings = set(schema["properties"]) - {"action", "id", "saturation"}
    bound = {
        name
        for part in PANEL["components"]
        if "action" in part
        for name, arg in PANEL["actions"][part["action"]["event"]["name"]]["args"].items()
        if isinstance(arg, dict) and not str(arg.get("path", "/")).startswith("/")
    }

    assert settings == {"on", "brightness", "hue", "kelvin"}
    assert settings <= bound


def test_the_guide_names_no_built_in_device_tool_or_page() -> None:
    assert "DeviceTool" not in GUIDE and "Devices page" not in GUIDE and "`kasa`" not in GUIDE
    assert "request_secret" in GUIDE and "packages" in GUIDE and "read_only_when" in GUIDE


def test_the_test_steps_say_a_device_that_answers_now_and_then_is_reported_not_rewritten() -> None:
    # In run 5 of the one-prompt Kasa rebuild, north called create_tool 9 times and spent 37 turns
    # adding retries for two lamps that stall after a change. Its code was not broken.
    steps = GUIDE.split("## Create it and prove it", 1)[1].split("\n## ", 1)[0]

    assert "some calls" in steps and "how often" in steps and "working code" in steps
