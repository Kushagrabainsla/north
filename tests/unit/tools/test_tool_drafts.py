"""What north tells a model about a tool, skill, flow or agent it is writing.

A model guessed how a tool's arguments arrive (north's own `temperature_converter` tried
`input.params`, then `input.parameters`, then the input itself), imported `requests`,
which north does not install, and wrote tools that freeze north or change things
without asking. Each mistake now comes back before the test call with the fix to make,
and every example in the authoring guides is a valid one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from flows.models import FlowSource
from flows.registry import parse_flow_document
from flows.validation import validate_flow_capabilities
from policies.self_edit import SelfEditPolicy
from skills.models import SkillIntent
from skills.registry import SkillRegistry, parse_execution_contract
from tools.base import Tool
from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.universal._draft_checks import draft_problems
from tools.universal.create_tool import CreateToolTool, _load_tool_file, _validate_tool_contract
from utils.runtime_resources import builtin_skills_dir

_GOOD = """
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

class LearnedDemo(Tool):
    name = "learned_demo"
    description = "demo"
    parameters_schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    async def run(self, input: ToolInput) -> ToolOutput:
        city = input.params.get("city", "Pune")
        return ToolOutput(success=True, data={"city": city})
"""


class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def all_tool_names(self) -> set[str]:
        return set(self.tools)


async def _create(tmp_path: Path, code: str):
    learned = tmp_path / "learned" / "tools"
    registry = _Registry()
    tool = CreateToolTool(
        tool_registry=registry, tools_dir=learned, self_edit_policy=SelfEditPolicy(learned, tmp_path / "m")
    )
    params = {"action": "create", "name": "learned_demo", "content": code, "test_params": {}}
    out = await tool.run(ToolInput(params=params))
    return out, registry, learned


@pytest.mark.parametrize(
    ("mistake", "draft", "fix"),
    [
        (
            "a package north does not have",
            _GOOD.replace("from tools.base", "import requests\nfrom tools.base"),
            "imports 'requests', which north does not have. Use httpx.AsyncClient",
        ),
        (
            "a guess at how arguments arrive",
            _GOOD.replace("input.params.get", "input.parameters.get"),
            "the call's arguments are the dict `input.params`",
        ),
        (
            "an exception for a bad call",
            _GOOD.replace('city = input.params.get("city", "Pune")', 'raise ValueError("city is required")'),
            "return `ToolOutput(success=False, error=...)`",
        ),
        (
            "a wait that freezes north",
            _GOOD.replace("from tools.base", "import time\nfrom tools.base").replace(
                "        city =", "        time.sleep(1)\n        city ="
            ),
            "freezes north while it waits: use await asyncio.sleep",
        ),
        (
            "a change north would never ask about",
            _GOOD.replace("        city =", "        Path('notes.txt').write_text('x')\n        city =").replace(
                "from tools.base", "from pathlib import Path\nfrom tools.base"
            ),
            "set `is_mutating = True` so north asks before it runs",
        ),
        (
            "a setting read from the environment",
            _GOOD.replace("from tools.base", "import os\nfrom tools.base").replace(
                '"Pune")', 'os.environ.get("CITY", "Pune"))'
            ),
            "use `from config.settings import settings`",
        ),
        (
            "a fixed path",
            _GOOD.replace('"Pune")', '"Pune")\n        notes = "~/.north/notes.json"'),
            "build paths from `settings.north_home`",
        ),
        (
            "a run that is not async",
            _GOOD.replace("async def run", "def run"),
            "must be `async def run(self, input: ToolInput) -> ToolOutput`",
        ),
    ],
)
async def test_a_draft_with_a_known_mistake_comes_back_with_the_fix(tmp_path, monkeypatch, mistake, draft, fix) -> None:
    monkeypatch.chdir(tmp_path)  # a draft north failed to stop would write here, not into the checkout
    out, registry, learned = await _create(tmp_path, draft)

    assert not out.success, mistake
    assert fix in out.error, out.error
    assert "learned_demo" not in registry.tools
    assert (learned / "candidates" / "specialized" / "learned_demo.py").exists(), "kept as a draft to update"


async def test_a_draft_without_those_mistakes_is_tested_and_switched_on(tmp_path) -> None:
    out, registry, _ = await _create(tmp_path, _GOOD)

    assert out.success, out.error
    assert "learned_demo" in registry.tools


async def test_a_call_that_forgets_the_name_uses_the_one_in_the_code(tmp_path) -> None:
    """Both guides' trial runs lost a round trip to "Parameter 'name' is required"."""
    learned = tmp_path / "learned" / "tools"
    registry = _Registry()
    tool = CreateToolTool(
        tool_registry=registry, tools_dir=learned, self_edit_policy=SelfEditPolicy(learned, tmp_path / "m")
    )
    call = ToolInput(params={"action": "create", "content": _GOOD, "test_params": {}})

    card = await tool.describe(call)
    out = await tool.run(call)

    assert "'learned_demo' tool" in card.message
    assert out.success, out.error
    assert "learned_demo" in registry.tools


async def test_creating_again_after_a_failed_try_replaces_the_draft(tmp_path) -> None:
    """After a failed test, the model called create again and was told its own draft 'already exists'."""
    failing = _GOOD.replace(
        'return ToolOutput(success=True, data={"city": city})', 'return ToolOutput(success=False, error="down")'
    )
    first, registry, _ = await _create(tmp_path, failing)
    again, registry, _ = await _create(tmp_path, _GOOD)

    assert not first.success
    assert again.success, again.error
    assert "learned_demo" in registry.tools


def test_a_tool_that_says_it_changes_things_may_change_them() -> None:
    declared = (
        _GOOD.replace('description = "demo"', 'description = "demo"\n    is_mutating = True')
        .replace("        city =", "        Path('notes.txt').write_text('x')\n        city =")
        .replace("from tools.base", "from pathlib import Path\nfrom tools.base")
    )
    assert draft_problems(declared) == []


# ── The examples in the authoring guides ─────────────────────────────────────


def _guide(name: str) -> str:
    return (builtin_skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _block(text: str, language: str, containing: str) -> str:
    for block in re.findall(rf"```{language}\n(.*?)```", text, re.S):
        if containing in block:
            return block
    raise AssertionError(f"no {language} example containing {containing!r}")


def test_the_tool_guides_example_passes_every_check(tmp_path) -> None:
    code = _block(_guide("adding-a-north-tool"), "python", "class GetWeatherTool")
    assert draft_problems(code) == []
    path = tmp_path / "get_weather.py"
    path.write_text(code, encoding="utf-8")
    _validate_tool_contract(_load_tool_file(path, expected_name="get_weather"))


def _skill_example() -> dict:
    return yaml.safe_load(_block(_guide("authoring-a-north-skill"), "yaml", "name: summarizing-saved-articles"))


def test_the_skill_guides_example_is_a_valid_create_skill_call() -> None:
    call = _skill_example()
    assert call["description"].startswith("Use when")
    assert set(call.get("intents", [])) <= {intent.value for intent in SkillIntent}
    execution = parse_execution_contract(call["execution"])
    assert execution is not None
    tools = ToolRegistry(auto_register=True).all_tool_names()
    assert set(execution.tools) <= tools
    assert len(call["positive_prompts"]) >= 3 and call["negative_prompts"]


def test_the_flow_guides_example_runs_the_skill_guides_example(tmp_path) -> None:
    skill = _skill_example()
    skill_dir = tmp_path / "skills" / skill["name"]
    skill_dir.mkdir(parents=True)
    frontmatter = {k: skill[k] for k in ("name", "description", "domains", "execution")}
    frontmatter |= {"source": "learned", "status": "active"}
    document = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n{skill['instructions']}\n"
    (skill_dir / "SKILL.md").write_text(document, encoding="utf-8")
    skills = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=tmp_path / "skills")

    call = yaml.safe_load(_block(_guide("authoring-a-north-flow"), "yaml", "action: create"))
    flow_document = yaml.safe_dump(
        {
            "name": call["name"],
            "description": call["description"],
            "source": "learned",
            "status": "candidate",
            "domains": ["general"],
            "steps": call["steps"],
        },
        sort_keys=False,
    )
    flow = parse_flow_document(flow_document, tmp_path / "flows" / call["name"], FlowSource.LEARNED)
    report = validate_flow_capabilities(flow, skill_registry=skills, tool_registry=ToolRegistry(auto_register=True))

    assert report.errors == ()
    assert call["test_inputs"]


def test_the_agent_guides_example_is_a_complete_create_agent_call() -> None:
    call = yaml.safe_load(_block(_guide("authoring-a-north-agent"), "yaml", "action: create"))
    assert re.fullmatch(r"[a-z][a-z0-9_]*", call["name"])
    assert call["description"] and call["accepts"] and call["system_prompt"].strip()
