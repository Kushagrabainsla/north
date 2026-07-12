"""Tests for fix 5b: conductor-mode prompt selection for the coder."""

from __future__ import annotations

import importlib
from pathlib import Path

from agents.models import AgentConfig, AgentDependencies
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


def _deps(tmp_path: Path) -> AgentDependencies:
    return AgentDependencies(
        context_store=FileContextStore(tmp_path / "ctx"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )


def _agent(name: str, tmp_path: Path):
    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    mod = importlib.import_module(f"agents.{name}.agent")
    cls = getattr(mod, config.resolved_class_name)
    return cls(config, _deps(tmp_path))


def test_coder_uses_principal_engineer_prompt(tmp_path):
    # The coder's single system.md is the principal-engineer prompt: it owns
    # verification end to end and does not self-delegate the review.
    prompt = _agent("coder", tmp_path)._load_system_prompt()
    assert "sole engineer" in prompt
    assert "do not delegate to the reviewer" in prompt.lower()
    assert "verify your own work" in prompt.lower()


def test_every_engineering_agent_loads_a_system_prompt(tmp_path):
    # Every agent loads exactly one prompt (system.md); there is no special-cased
    # second prompt file anymore.
    for name in ("researcher", "architect", "coder", "reviewer"):
        prompt = _agent(name, tmp_path)._load_system_prompt()
        assert isinstance(prompt, str) and prompt.strip()
        assert not (AGENTS_DIR / name / "prompts" / "system_conductor.md").exists()


def test_clean_code_rules_injected_into_coder_and_reviewer(tmp_path):
    # One shared clean-code file, always appended to the two code agents' prompts.
    marker = "Clean code (apply aggressively)"
    for name in ("coder", "reviewer"):
        assert marker in _agent(name, tmp_path)._load_system_prompt(), f"{name} missing clean-code rules"


def test_clean_code_rules_not_injected_into_non_code_agents(tmp_path):
    # Gated to coder + reviewer only - not every engineering agent, not general.
    marker = "Clean code (apply aggressively)"
    for name in ("researcher", "architect", "general"):
        assert marker not in _agent(name, tmp_path)._load_system_prompt(), f"{name} should not carry clean-code rules"


def test_safety_policy_injected_into_every_agent(tmp_path):
    # The global safety policy (policies/safety.md, applies_to "*") binds every
    # agent - code and domain alike - via the policy primitive.
    marker = "safety (non-negotiable)"
    for name in ("coder", "reviewer", "researcher", "general", "wellness"):
        assert marker in _agent(name, tmp_path)._load_system_prompt(), f"{name} missing the safety policy"


def test_coder_prompt_has_the_god_prompt_rules(tmp_path):
    # Devin/Claude Code gaps now always-on in the coder: don't cheat tests,
    # don't assume libraries, don't create unasked files.
    prompt = _agent("coder", tmp_path)._load_system_prompt().lower()
    assert "make a failure disappear" in prompt or "weaken or delete an assertion" in prompt
    assert "third-party library" in prompt or "dependency change" in prompt
    assert "unsolicited artifacts" in prompt or "readme" in prompt
