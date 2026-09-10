from __future__ import annotations

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
