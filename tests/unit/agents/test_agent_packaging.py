from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents.exceptions import AgentConfigError
from agents.packaging import agent_module_root
from agents.registry import AgentRegistry


def test_agent_module_root_is_derived_from_its_own_package() -> None:
    assert agent_module_root() == "agents"


def _scaffold_agent(root: Path, name: str) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        f"agent: {name}\ndomain: testing\nmodel_pool: reasoning\n",
        encoding="utf-8",
    )
    (agent_dir / "agent.py").write_text("", encoding="utf-8")
    return agent_dir


def test_registry_resolves_agent_modules_through_the_configured_root(tmp_path, monkeypatch) -> None:
    """A relocated package must not leave agent discovery importing the old prefix."""
    _scaffold_agent(tmp_path, "widget")
    monkeypatch.setattr("agents.registry.agent_module_root", lambda: "relocated_pkg.agents")

    with pytest.raises(AgentConfigError) as failure:
        AgentRegistry(agents_dir=tmp_path, deps=object())

    assert "relocated_pkg.agents.widget.agent" in str(failure.value)


def test_registry_loads_user_agents_from_a_separate_root(tmp_path) -> None:
    user_root = tmp_path / "user-agents"
    agent_dir = user_root / "widget"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "agent: widget\ndomain: testing\nmodel_pool: reasoning\nclass_name: WidgetAgent\n",
        encoding="utf-8",
    )
    (agent_dir / "agent.py").write_text(
        "from agents.llm_agent import LLMAgent\n\nclass WidgetAgent(LLMAgent):\n    pass\n",
        encoding="utf-8",
    )
    (agent_dir / "prompts" / "system.md").write_text("You are a widget specialist.", encoding="utf-8")

    registry = AgentRegistry(agents_dir=tmp_path / "builtins", user_agents_dir=user_root, deps=object())

    assert registry.names() == ["widget"]
    assert registry.get("widget").name == "widget"


def test_packaged_agents_take_precedence_over_user_name_collisions(tmp_path) -> None:
    user_root = tmp_path / "user-agents"
    agent_dir = user_root / "general"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "agent: general\ndomain: user\nmodel_pool: reasoning\nclass_name: GeneralAgent\n",
        encoding="utf-8",
    )
    (agent_dir / "agent.py").write_text(
        "raise RuntimeError('a colliding personal agent must not execute')\n",
        encoding="utf-8",
    )
    (agent_dir / "prompts" / "system.md").write_text("User override.", encoding="utf-8")

    builtin_root = Path(__file__).parents[3] / "agents"
    registry = AgentRegistry(agents_dir=builtin_root, user_agents_dir=user_root, deps=object())

    assert registry.get("general").domain != "user"


def test_broken_personal_agent_does_not_prevent_registry_startup(tmp_path, caplog) -> None:
    user_root = tmp_path / "user-agents"
    agent_dir = user_root / "broken"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "agent: broken\ndomain: testing\nclass_name: BrokenAgent\n",
        encoding="utf-8",
    )
    (agent_dir / "agent.py").write_text("raise RuntimeError('broken extension')\n", encoding="utf-8")

    registry = AgentRegistry(agents_dir=tmp_path / "builtins", user_agents_dir=user_root, deps=object())

    assert registry.names() == []
    assert "failed to load personal agent broken" in caplog.text
    assert "north_user_agents.broken.agent" not in sys.modules
