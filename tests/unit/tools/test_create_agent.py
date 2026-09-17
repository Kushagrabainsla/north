from __future__ import annotations

from pathlib import Path

from agents.models import AgentConfig
from tools.universal.create_agent import CreateAgentTool, _list_agents, _read_agent


def test_agent_tool_listing_merges_builtins_and_user_agents(tmp_path: Path) -> None:
    user_root = tmp_path / "agents"
    user_agent = user_root / "custom"
    user_agent.mkdir(parents=True)
    (user_agent / "config.yaml").write_text(
        "agent: custom\ndomain: testing\naccepts: [custom]\n",
        encoding="utf-8",
    )

    result = _list_agents(user_root)
    names = [agent["name"] for agent in result.data["agents"]]

    assert "general" in names
    assert "custom" in names


def test_agent_tool_read_prefers_packaged_agent_on_name_collision(tmp_path: Path) -> None:
    user_agent = tmp_path / "general"
    user_agent.mkdir(parents=True)
    (user_agent / "config.yaml").write_text("agent: general\ndomain: user\n", encoding="utf-8")
    (user_agent / "prompts").mkdir()
    (user_agent / "prompts" / "system.md").write_text("user override", encoding="utf-8")

    result = _read_agent("general", tmp_path)

    assert result.success
    assert "domain: user" not in result.data["config"]


def test_agent_tool_safely_serializes_untrusted_text(tmp_path: Path) -> None:
    tool = CreateAgentTool(agents_dir=tmp_path)
    result = tool._create(
        {
            "name": "safe_agent",
            "description": 'Ends a docstring """ and must not become Python.',
            "domain": "quality: review",
            "model_pool": "reasoning",
            "accepts": ['quoted "value"', "colon: value", "line\nbreak"],
            "system_prompt": "Review carefully.",
        }
    )

    assert result.success
    agent_dir = tmp_path / "safe_agent"
    source = (agent_dir / "agent.py").read_text(encoding="utf-8")
    compile(source, str(agent_dir / "agent.py"), "exec")
    config = AgentConfig.from_yaml(agent_dir / "config.yaml")
    assert config.domain == "quality: review"
    assert config.accepts == ['quoted "value"', "colon: value", "line\nbreak"]
