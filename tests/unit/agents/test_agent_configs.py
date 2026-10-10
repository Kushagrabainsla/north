"""Validate the static artifacts of every built-in agent.

Config files and system prompts are checked before any LLM call is made, so a regression in them is caught
cheaply. No network calls, no async.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"
AGENTS = sorted(path.name for path in AGENTS_DIR.iterdir() if (path / "config.yaml").is_file())


def test_the_in_house_coding_agents_are_gone() -> None:
    """north does not write code itself: the coding agents are the user's installed ones (`coding_agent`)."""
    assert not {"coder", "architect", "reviewer", "researcher"} & set(AGENTS)


@pytest.mark.parametrize("name", AGENTS)
def test_config_loads(name: str) -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    assert config.agent == name
    assert config.domain


@pytest.mark.parametrize("name", AGENTS)
def test_config_class_name_resolves(name: str) -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    assert config.resolved_class_name


@pytest.mark.parametrize("name", AGENTS)
def test_agent_instantiates(name: str, tmp_path: Path) -> None:
    from agents.models import AgentConfig, AgentDependencies
    from memory import FileContextStore
    from tests.conftest import MockInferenceRouter
    from tools.confidence import ConfidenceTracker
    from tools.registry import ToolRegistry

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )

    cls = getattr(importlib.import_module(f"agents.{name}.agent"), config.resolved_class_name)
    agent = cls(config, deps)

    assert agent.name == name
    assert agent.domain == config.domain


@pytest.mark.parametrize("name", AGENTS)
def test_system_prompt_exists_and_is_not_trivial(name: str) -> None:
    prompt = AGENTS_DIR / name / "prompts" / "system.md"
    assert prompt.exists(), f"Missing system prompt: {prompt}"
    assert len(prompt.read_text(encoding="utf-8").strip()) > 200, f"{name} system prompt is suspiciously short"


@pytest.mark.parametrize("name", AGENTS)
def test_agents_do_not_own_tool_allowlists(name: str) -> None:
    """Tools are selected from one global catalog at task time."""
    assert not (AGENTS_DIR / name / "tools.yaml").exists()


def test_general_prompt_requires_flow_intake_before_creation() -> None:
    prompt = (AGENTS_DIR / "general" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "resolve all missing setup choices with `ask_user`" in prompt
    assert "manual only, one-time, or recurring" in prompt
    assert "before `create_flow:create`" in prompt
    assert "never silently assume manual use or invent a schedule" in prompt
    assert "Collecting schedule details does not authorize testing, activation, or installation" in prompt
    assert "Timezone is not a flow input or question" in prompt
    assert "configured in North settings" in prompt


@pytest.mark.parametrize(
    "relative_path",
    [
        "agents/general/prompts/system.md",
        "resources/builtin-skills/browser-research-and-extraction/SKILL.md",
        "resources/builtin-skills/authoring-a-north-flow/SKILL.md",
    ],
)
def test_browser_prompts_select_profile_and_reuse_existing_login(relative_path: str) -> None:
    prompt = (AGENTS_DIR.parent / relative_path).read_text(encoding="utf-8")
    assert "which browser profile" in prompt
    assert "already signed in" in prompt
    assert "Never silently switch" in prompt
    assert "read or export passwords" in prompt
    assert "requires_user_action=true" in prompt
    assert "inspect and assert" in prompt
