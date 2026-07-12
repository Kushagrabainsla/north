"""Tests for fix 2a: agents record which model(s) they used (models_used).

Foundation for the second-model review guarantee and the Definition-of-Done
gate - both need to know, from recorded evidence, which model produced a result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from inference.models import ToolCall, ToolCallResponse
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


def _make_deps(tmp_path: Path, router: MockInferenceRouter | None = None) -> AgentDependencies:
    return AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=router or MockInferenceRouter(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )


def _load_agent(name: str, tmp_path: Path, router: MockInferenceRouter | None = None):
    import importlib

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    mod = importlib.import_module(f"agents.{name}.agent")
    cls = getattr(mod, config.resolved_class_name)
    return cls(config, _make_deps(tmp_path, router))


def test_agent_result_models_used_defaults_empty():
    r = AgentResult(output="x", summary="x")
    assert r.models_used == []


async def test_single_model_is_captured(tmp_path: Path):
    # The default mock router answers with model_used="mock-model".
    agent = _load_agent("coder", tmp_path)
    result = await agent.run(AgentPayload(task_id="t1", prompt="Implement x."))
    assert result.models_used == ["mock-model"]


async def test_models_used_dedups_and_preserves_order(tmp_path: Path):
    call = 0

    class MultiModelRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal call
            call += 1
            if call == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="missing", call_id="c1", params={})],
                    model_used="model-a",
                )
            if call == 2:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="missing", call_id="c2", params={})],
                    model_used="model-a",  # repeat - must not duplicate
                )
            text = "Done."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(type="message", content=text, calls=[], model_used="model-b")

    agent = _load_agent("coder", tmp_path, MultiModelRouter())
    result = await agent.run(AgentPayload(task_id="t2", prompt="Implement x."))
    assert result.models_used == ["model-a", "model-b"]


async def test_empty_model_used_is_ignored(tmp_path: Path):
    class BlankModelRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            text = "Done."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(type="message", content=text, calls=[], model_used="")

    agent = _load_agent("reviewer", tmp_path, BlankModelRouter())
    result = await agent.run(AgentPayload(task_id="t3", prompt="Review."))
    assert result.models_used == []


@pytest.mark.parametrize("name", ["architect", "coder", "researcher", "reviewer"])
async def test_all_engineering_agents_capture_models(name: str, tmp_path: Path):
    agent = _load_agent(name, tmp_path)
    result = await agent.run(AgentPayload(task_id="t1", prompt="do work"))
    assert result.models_used == ["mock-model"]
