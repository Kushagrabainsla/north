"""Validate static artifacts for the 4 engineering agents.

Tests that config.yaml files and system prompts are
structurally correct so regressions in these files are caught before any
LLM call is made.  No network calls, no async.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"
ENGINEERING_AGENTS = ["architect", "coder", "researcher", "reviewer"]


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_config_loads(name: str) -> None:
    """config.yaml must parse without error for every engineering agent."""
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    assert config.agent == name
    assert config.domain == "engineering"


@pytest.mark.parametrize("name", ["architect", "coder", "researcher", "reviewer"])
def test_implementation_agents_use_reasoning_pool(name: str) -> None:
    """Engineering and reasoning tasks resolve to the reasoning model pool dynamically."""
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._north_settings = None
    pool = orch._resolve_task_model_pool(domain="engineering")
    assert pool == "reasoning"


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_config_declares_produces(name: str) -> None:
    """Every agent must declare at least one artifact it produces."""
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    assert len(config.produces) >= 1, f"{name} declares no produces"


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_config_class_name_resolves(name: str) -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    assert config.resolved_class_name  # non-empty string


def test_architect_class_name() -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / "architect" / "config.yaml")
    assert config.resolved_class_name == "ArchitectAgent"


def test_coder_class_name() -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / "coder" / "config.yaml")
    assert config.resolved_class_name == "CoderAgent"


def test_researcher_class_name() -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / "researcher" / "config.yaml")
    assert config.resolved_class_name == "ResearcherAgent"


def test_reviewer_class_name() -> None:
    from agents.models import AgentConfig

    config = AgentConfig.from_yaml(AGENTS_DIR / "reviewer" / "config.yaml")
    assert config.resolved_class_name == "ReviewerAgent"


# ---------------------------------------------------------------------------
# Agent instantiation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_agent_instantiates(name: str, tmp_path: Path) -> None:
    """Every engineering agent must instantiate without error."""
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

    # Import the agent class dynamically
    import importlib

    mod = importlib.import_module(f"agents.{name}.agent")
    cls = getattr(mod, config.resolved_class_name)
    agent = cls(config, deps)

    assert agent.name == name
    assert agent.domain == "engineering"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_system_prompt_exists(name: str) -> None:
    prompt_path = AGENTS_DIR / name / "prompts" / "system.md"
    assert prompt_path.exists(), f"Missing system prompt: {prompt_path}"


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_system_prompt_non_empty(name: str) -> None:
    content = (AGENTS_DIR / name / "prompts" / "system.md").read_text(encoding="utf-8")
    assert len(content.strip()) > 200, f"{name} system prompt is suspiciously short"


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_system_prompt_references_all_team_members(name: str) -> None:
    """Each agent's system prompt must mention all 4 engineering agents by name."""
    content = (AGENTS_DIR / name / "prompts" / "system.md").read_text(encoding="utf-8")
    for teammate in ENGINEERING_AGENTS:
        assert teammate in content, f"{name}'s prompt must mention '{teammate}'"


def test_architect_prompt_produces_spec_and_decision_log() -> None:
    content = (AGENTS_DIR / "architect" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "spec.md" in content
    assert "decision_log.md" in content


def test_coder_prompt_produces_implementation_notes() -> None:
    content = (AGENTS_DIR / "coder" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "implementation_notes.md" in content


def test_researcher_prompt_produces_context_md_and_references_json() -> None:
    content = (AGENTS_DIR / "researcher" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "context.md" in content
    assert "references.json" in content


def test_reviewer_prompt_produces_review_reports() -> None:
    content = (AGENTS_DIR / "reviewer" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "review_report_latest.md" in content


def test_coder_prompt_always_delegates_to_reviewer() -> None:
    """Coder must state it always hands off to reviewer - no exceptions."""
    content = (AGENTS_DIR / "coder" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "reviewer" in content.lower()
    assert "always" in content.lower()


def test_reviewer_prompt_adversarial_posture() -> None:
    content = (AGENTS_DIR / "reviewer" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "adversarial" in content.lower()


def test_architect_prompt_routes_based_on_task_verb() -> None:
    """Architect prompt must include delegation routing table for build vs design tasks."""
    content = (AGENTS_DIR / "architect" / "prompts" / "system.md").read_text(encoding="utf-8")
    # Both "design" (stop) and "build" (delegate) must be in the routing section
    assert "design" in content.lower()
    assert "build" in content.lower()
    assert "delegate" in content.lower()


def test_researcher_prompt_routes_based_on_task_verb() -> None:
    """Researcher prompt must include delegation routing table for research vs build tasks."""
    content = (AGENTS_DIR / "researcher" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "research" in content.lower()
    assert "build" in content.lower()
    assert "delegate" in content.lower()


def test_reviewer_prompt_routes_code_bugs_to_coder() -> None:
    content = (AGENTS_DIR / "reviewer" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "coder" in content.lower()
    assert "code bug" in content.lower() or "bug" in content.lower()


def test_reviewer_prompt_routes_spec_gaps_to_architect() -> None:
    content = (AGENTS_DIR / "reviewer" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "architect" in content.lower()
    assert "spec" in content.lower()


# ---------------------------------------------------------------------------
# Tool eligibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ENGINEERING_AGENTS)
def test_agents_do_not_own_tool_allowlists(name: str) -> None:
    """Tools are selected from one global catalog at task time."""
    assert not (AGENTS_DIR / name / "tools.yaml").exists()
