"""Unit tests for dynamic platform capabilities synthesizer (agents/capabilities.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.capabilities import build_platform_capabilities_summary


def test_build_platform_capabilities_summary_empty_deps() -> None:
    deps = MagicMock()
    deps.agent_registry = None
    deps.tool_registry = None
    deps.skill_registry = None
    deps.flow_registry = None

    summary = build_platform_capabilities_summary(deps)
    assert "Platform Capabilities & Ecosystem Overview" in summary


def test_build_platform_capabilities_summary_with_registries() -> None:
    deps = MagicMock()

    # Mock agent
    mock_agent = MagicMock()
    mock_agent.name = "coder"
    mock_agent.domain = "engineering"
    mock_agent.config.accepts = ["code", "bugfix"]
    deps.agent_registry.all.return_value = [mock_agent]

    # Mock tool
    mock_tool = MagicMock()
    mock_tool.name = "gh"
    mock_tool.description = "Run GitHub operations via the gh CLI."
    deps.tool_registry.all_tools.return_value = [mock_tool]

    # Mock skill
    mock_skill = MagicMock()
    mock_skill.name = "debug_workflow"
    mock_skill.description = "Automated bug investigation workflow"
    deps.skill_registry.all.return_value = [mock_skill]

    mock_flow = MagicMock()
    mock_flow.name = "job-application-review"
    mock_flow.description = "Prepare job applications for review."
    mock_flow.status = "active"
    mock_flow.steps = [MagicMock(), MagicMock()]
    deps.flow_registry.all.return_value = [mock_flow]

    summary = build_platform_capabilities_summary(deps)

    assert "coder" in summary
    assert "gh" in summary
    assert "debug_workflow" in summary
    assert "job-application-review" in summary
    assert "active, 2 steps" in summary
    assert "Run GitHub operations via the gh CLI" not in summary
    assert "Available Tool Names" in summary
    assert "find_tools" in summary
    assert "create_tool" in summary
    assert "create_agent" in summary
