"""Tests for curated preferred-model routing and per-task model stickiness.

These exercise the REAL dispatcher routing logic through the same Provider
interface production uses (fake catalogs, not a mocked router), so the behaviour
covered here is exactly the behaviour that ships.
"""

from __future__ import annotations

import logging

import pytest

from config.strategy import NorthSettings
from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import InferenceError
from inference.model_policy import model_matches, parse_preferred, split_spec
from inference.models import CompletionRequest, CompletionResponse, PoolPriority

# ---------------------------------------------------------------- pure helpers


def test_model_matches_family():
    # A family token matches version-suffixed ids (contiguous substring)...
    assert model_matches("claude-sonnet", "openrouter", "anthropic/claude-sonnet-5")
    assert model_matches("claude-sonnet", "openrouter", "anthropic/claude-sonnet-4.6")
    # ...matches across provider id schemes (openrouter vs the gemini direct api)...
    assert model_matches("gemini-2.5-pro", "openrouter", "google/gemini-2.5-pro")
    assert model_matches("gemini-2.5-pro", "gemini", "models/gemini-2.5-pro")
    # ...but does NOT match a different family.
    assert not model_matches("claude-sonnet", "openrouter", "anthropic/claude-3.5-haiku")
    assert not model_matches("gemini-2.5-pro", "gemini", "models/gemini-2.5-flash")


def test_model_matches_rejects_numeric_overmatch():
    # The critical bug this guards: an independent-words match wrongly accepted
    # gpt-3.5-turbo for "gpt-5" (because "5" occurs in "3.5") and nemotron-49b for
    # "llama-4". Contiguous matching rejects both.
    assert not model_matches("gpt-5", "openrouter", "openai/gpt-3.5-turbo-0613")
    assert model_matches("gpt-5", "openrouter", "openai/gpt-5.6-luna")
    assert not model_matches("llama-4-scout", "openrouter", "nvidia/llama-3.3-nemotron-super-49b")
    assert model_matches("llama-4-scout", "groq", "meta-llama/llama-4-scout-17b-16e-instruct")


def test_model_matches_provider_qualifier():
    assert model_matches("groq:llama-3.3", "groq", "llama-3.3-70b-versatile")
    # Provider qualifier must match the provider name.
    assert not model_matches("groq:llama-3.3", "openrouter", "meta/llama-3.3-70b")


def test_split_spec():
    assert split_spec("openrouter:anthropic/claude-sonnet") == ("openrouter", "anthropic/claude-sonnet")
    # A bare OpenRouter id (contains '/') is NOT treated as provider-qualified.
    assert split_spec("anthropic/claude-sonnet") == (None, "anthropic/claude-sonnet")
    assert split_spec("gpt-4o") == (None, "gpt-4o")


def test_parse_preferred():
    assert parse_preferred({"reasoning": ["a", "b"]}) == {"reasoning": ["a", "b"]}
    # comma-separated string form
    assert parse_preferred({"reasoning": "a, b ,c"}) == {"reasoning": ["a", "b", "c"]}
    # junk / wrong types degrade to empty rather than raising
    assert parse_preferred("nonsense") == {}
    assert parse_preferred({"reasoning": 5}) == {}
    assert parse_preferred({"reasoning": []}) == {}


# ---------------------------------------------------------------- test doubles


def _mi(
    model_id: str,
    *,
    provider: str = "p",
    quality: float = 0.5,
    cost: float = 0.0,
    ctx: int = 100_000,
    caps: frozenset[ModelCapability] | None = None,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider_name=provider,
        capabilities=caps or frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        context_window=ctx,
        cost_per_token=cost,
        base_quality=quality,
    )


def _resp(model: str) -> CompletionResponse:
    return CompletionResponse(text="ok", model_used=model, tokens_in=1, tokens_out=1, cost_usd=0.0)


class _Catalog:
    """A fake Provider exposing an arbitrary catalog, with a per-model responder."""

    def __init__(self, name: str, models: list[ModelInfo], responder) -> None:
        self._name = name
        self._models = {m.model_id: m for m in models}
        self._responder = responder
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(model_id)
        return self._responder(model_id, request)

    async def refresh(self) -> None:
        pass


def _disp(provider: _Catalog, tmp_path, preferred: dict[str, list[str]] | None = None) -> ModelDispatcher:
    ns = NorthSettings(tmp_path / "settings.json", default_preferred_models=preferred or {})
    return ModelDispatcher(providers=[provider], north_settings=ns, cooldowns_path=tmp_path / "cd.json")


# ---------------------------------------------------------------- promotion


@pytest.mark.asyncio
async def test_preferred_model_promoted_over_higher_priced(tmp_path):
    # 'budget-star' is cheaper/lower base_quality but is the preferred coding model;
    # it must win over the pricier 'expensive' one for a HIGH (reasoning) call.
    cat = _Catalog(
        "p",
        [_mi("expensive", quality=0.9), _mi("budget-star", quality=0.2)],
        lambda m, r: _resp(m),
    )
    disp = _disp(cat, tmp_path, preferred={"reasoning": ["budget-star"]})
    resp = await disp.complete(CompletionRequest(prompt="code", priority=PoolPriority.HIGH, component="coder"))
    assert resp.model_used == "budget-star"


@pytest.mark.asyncio
async def test_no_matching_preferred_falls_back_to_price(tmp_path, caplog):
    cat = _Catalog(
        "p",
        [_mi("expensive", quality=0.9), _mi("cheap", quality=0.2)],
        lambda m, r: _resp(m),
    )
    with caplog.at_level(logging.WARNING):
        disp = _disp(cat, tmp_path, preferred={"reasoning": ["nonexistent-model"]})
    # Stale spec is surfaced at construction, then price ranking is used.
    assert any("matches no model" in rec.message for rec in caplog.records)
    resp = await disp.complete(CompletionRequest(prompt="code", priority=PoolPriority.HIGH, component="coder"))
    assert resp.model_used == "expensive"


@pytest.mark.asyncio
async def test_preferred_not_applied_to_low_priority(tmp_path):
    # high_volume/LOW (and the ECO strategy that maps to it) must never be
    # overridden - cheapest-first still wins even if a costly model is 'preferred'.
    cat = _Catalog(
        "p",
        [_mi("pricey-preferred", quality=0.9, cost=0.01), _mi("cheapest", quality=0.2, cost=0.0)],
        lambda m, r: _resp(m),
    )
    disp = _disp(cat, tmp_path, preferred={"reasoning": ["pricey-preferred"]})
    resp = await disp.complete(CompletionRequest(prompt="bg", priority=PoolPriority.LOW, component="compact"))
    assert resp.model_used == "cheapest"


@pytest.mark.asyncio
async def test_unhealthy_preferred_is_demoted(tmp_path):
    # A preferred model that keeps failing stops being tried first after enough
    # failures; the reliable price-fallback model then leads the queue.
    def responder(model_id, request):
        if model_id == "cheap-preferred":
            raise InferenceError("boom")
        return _resp(model_id)

    cat = _Catalog("p", [_mi("expensive", quality=0.9), _mi("cheap-preferred", quality=0.2)], responder)
    disp = _disp(cat, tmp_path, preferred={"reasoning": ["cheap-preferred"]})

    # 5 calls: the failing preferred model is tried first each time, then falls
    # through to 'expensive'. That is enough failures to drop it below the floor.
    for _ in range(5):
        resp = await disp.complete(CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder"))
        assert resp.model_used == "expensive"
    assert cat.calls.count("cheap-preferred") == 5

    # 6th call: no longer promoted, so 'expensive' (higher price-quality) leads and
    # the demoted model is not tried first anymore.
    await disp.complete(CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder"))
    assert cat.calls.count("cheap-preferred") == 5  # unchanged - it was not tried again


# ---------------------------------------------------------------- stickiness


@pytest.mark.asyncio
async def test_task_sticky_model_reused(tmp_path):
    # Pre-pin a lower-quality model for this task; it must be reused over the
    # higher-quality one, proving stickiness reorders candidates.
    cat = _Catalog("p", [_mi("m-high", quality=0.9), _mi("m-low", quality=0.2)], lambda m, r: _resp(m))
    disp = _disp(cat, tmp_path)
    key = ("t1", "coder", ModelCapability.COMPLETION.value, PoolPriority.HIGH.value)
    disp._sticky[key] = ("m-low", "p")

    resp = await disp.complete(
        CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder", task_id="t1")
    )
    assert resp.model_used == "m-low"


@pytest.mark.asyncio
async def test_sticky_recorded_then_refreshed_on_failure(tmp_path):
    state = {"m1_fails": False}

    def responder(model_id, request):
        if model_id == "m1" and state["m1_fails"]:
            raise InferenceError("m1 down")
        return _resp(model_id)

    cat = _Catalog("p", [_mi("m1", quality=0.9), _mi("m2", quality=0.5)], responder)
    disp = _disp(cat, tmp_path)
    key = ("t1", "coder", ModelCapability.COMPLETION.value, PoolPriority.HIGH.value)

    # First call picks the best model and remembers it for the task.
    r1 = await disp.complete(
        CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder", task_id="t1")
    )
    assert r1.model_used == "m1"
    assert disp._sticky[key] == ("m1", "p")

    # m1 goes down: the task falls through to m2 and the pin is refreshed to m2.
    state["m1_fails"] = True
    r2 = await disp.complete(
        CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder", task_id="t1")
    )
    assert r2.model_used == "m2"
    assert disp._sticky[key] == ("m2", "p")


@pytest.mark.asyncio
async def test_no_task_id_means_no_stickiness(tmp_path):
    cat = _Catalog("p", [_mi("m1", quality=0.9)], lambda m, r: _resp(m))
    disp = _disp(cat, tmp_path)
    await disp.complete(CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="planner"))
    assert len(disp._sticky) == 0


@pytest.mark.asyncio
async def test_sticky_map_is_bounded(tmp_path):
    from inference.constants import _STICKY_MAX_ENTRIES

    cat = _Catalog("p", [_mi("m1", quality=0.9)], lambda m, r: _resp(m))
    disp = _disp(cat, tmp_path)
    for i in range(_STICKY_MAX_ENTRIES + 25):
        await disp.complete(
            CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder", task_id=f"task-{i}")
        )
    assert len(disp._sticky) <= _STICKY_MAX_ENTRIES


@pytest.mark.asyncio
async def test_no_north_settings_keeps_price_behaviour(tmp_path):
    # A dispatcher built without NorthSettings has no preferred lists and behaves
    # exactly as before (highest price-quality first). Guards test/prod parity.
    cat = _Catalog("p", [_mi("expensive", quality=0.9), _mi("cheap", quality=0.2)], lambda m, r: _resp(m))
    disp = ModelDispatcher(providers=[cat], cooldowns_path=tmp_path / "cd.json")
    resp = await disp.complete(CompletionRequest(prompt="c", priority=PoolPriority.HIGH, component="coder"))
    assert resp.model_used == "expensive"
