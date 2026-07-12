"""Tests for fix 2d: different-model reviewer via explicit exclude_models.

Covers the dispatcher exclusion primitive, the agent threading it into requests,
and the orchestrator translating an agent's `distinct_from` config into the
excluded model(s) for a task.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agents.models import AgentConfig, AgentDependencies, AgentPayload
from approval.store import ApprovalStore
from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.models import CompletionRequest, CompletionResponse, PoolPriority, ToolCallResponse
from ledger.models import LedgerEntry, LedgerSource
from memory import FileContextStore
from orchestrator.orchestrator import Orchestrator
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


# ---------------------------------------------------------------- dispatcher primitive


def _mi(model_id: str, quality: float) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider_name="p",
        capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        context_window=100_000,
        cost_per_token=0.0,
        base_quality=quality,
    )


class _Catalog:
    def __init__(self, models, responder):
        self._models = {m.model_id: m for m in models}
        self.responder = responder
        self.calls: list[str] = []

    @property
    def name(self):
        return "p"

    def get_models(self):
        return dict(self._models)

    async def complete(self, model_id, request):
        self.calls.append(model_id)
        return self.responder(model_id)

    async def refresh(self):
        pass


def _resp(m):
    return CompletionResponse(text="ok", model_used=m, tokens_in=1, tokens_out=1, cost_usd=0.0)


async def test_exclude_models_skips_excluded(tmp_path):
    cat = _Catalog([_mi("model-a", 0.9), _mi("model-b", 0.5)], _resp)
    disp = ModelDispatcher(providers=[cat], cooldowns_path=tmp_path / "cd.json")
    # model-a ranks first, but is excluded → model-b answers.
    r = await disp.complete(
        CompletionRequest(prompt="x", priority=PoolPriority.HIGH, component="reviewer", exclude_models=["model-a"])
    )
    assert r.model_used == "model-b"


async def test_exclude_models_degrades_when_only_excluded_left(tmp_path):
    cat = _Catalog([_mi("only-model", 0.9)], _resp)
    disp = ModelDispatcher(providers=[cat], cooldowns_path=tmp_path / "cd.json")
    # Excluding the only model must NOT block - it degrades to using it.
    r = await disp.complete(
        CompletionRequest(prompt="x", priority=PoolPriority.HIGH, component="reviewer", exclude_models=["only-model"])
    )
    assert r.model_used == "only-model"


async def test_exclude_models_is_case_insensitive(tmp_path):
    cat = _Catalog([_mi("Model-A", 0.9), _mi("model-b", 0.5)], _resp)
    disp = ModelDispatcher(providers=[cat], cooldowns_path=tmp_path / "cd.json")
    r = await disp.complete(
        CompletionRequest(prompt="x", priority=PoolPriority.HIGH, component="reviewer", exclude_models=["model-a"])
    )
    assert r.model_used == "model-b"


# ---------------------------------------------------------------- agent threading


async def test_agent_threads_exclude_models_into_request(tmp_path):
    seen = {}

    class InspectingRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            seen["exclude_models"] = request.exclude_models
            return ToolCallResponse(type="message", content="done", calls=[], model_used="mock-model")

    config = AgentConfig.from_yaml(AGENTS_DIR / "reviewer" / "config.yaml")
    import importlib

    mod = importlib.import_module("agents.reviewer.agent")
    agent = mod.ReviewerAgent(
        config,
        AgentDependencies(
            context_store=FileContextStore(tmp_path / "ctx"),
            inference_router=InspectingRouter(),
            tool_registry=ToolRegistry(graph={}, auto_register=False),
            confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        ),
    )
    await agent.run(AgentPayload(task_id="t1", prompt="review", exclude_models=["coder-model"]))
    assert seen["exclude_models"] == ["coder-model"]


# ---------------------------------------------------------------- orchestrator translation


def _orch(entries):
    ledger = MagicMock()
    ledger.query = AsyncMock(return_value=entries)
    ledger.write = AsyncMock()
    stream = MagicMock()
    stream.emit = AsyncMock()
    return Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream,
        approval_store=ApprovalStore(),
    )


def _agent(name: str, distinct_from: list[str]):
    a = MagicMock()
    a.name = name
    a.config = MagicMock()
    a.config.distinct_from = distinct_from
    return a


def _completed(agent: str, model: str) -> LedgerEntry:
    return LedgerEntry.new(
        source=LedgerSource.AGENT, task_id="t1", agent=agent, action="agent_completed", model_used=model
    )


async def test_exclude_models_for_returns_coder_model():
    orch = _orch([_completed("coder", "model-a")])
    reviewer = _agent("reviewer", ["coder"])
    assert await orch._exclude_models_for("t1", reviewer) == ["model-a"]


async def test_exclude_models_for_no_distinct_from_skips_query():
    orch = _orch([])
    coder = _agent("coder", [])
    assert await orch._exclude_models_for("t1", coder) == []
    orch._ledger.query.assert_not_called()


async def test_exclude_models_for_dedups_multiple_models():
    orch = _orch([_completed("coder", "model-a, model-b"), _completed("coder", "model-a")])
    reviewer = _agent("reviewer", ["coder"])
    assert await orch._exclude_models_for("t1", reviewer) == ["model-a", "model-b"]
