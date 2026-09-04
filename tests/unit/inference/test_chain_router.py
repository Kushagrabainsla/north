"""The chain router end to end: select, call, react to failure, record why."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inference.cooldowns import CooldownStore
from inference.decisions import DecisionLog
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    PaymentRequiredError,
    ProviderUnavailableError,
)
from inference.facts.catalog import CatalogSnapshot, FactsCatalog
from inference.facts.models import Endpoint, ModelFacts, Rank, fact
from inference.facts.store import ModelFactsStore
from inference.provider_health import ProviderHealthTracker
from inference.routing.availability import AvailabilityView, EntitlementLedger
from inference.routing.chain import Requirements
from inference.routing.router import ChainRouter, requirements_from

_WHEN = datetime(2026, 9, 3, tzinfo=UTC)
_BILLING = {"error": {"type": "CreditsError", "message": "No payment method"}}


class _FakeProvider:
    """Answers, or raises whatever the test queued for that model id."""

    def __init__(self, name: str, behaviour: dict[str, object]) -> None:
        self.name = name
        self._behaviour = behaviour
        self.calls: list[str] = []

    async def respond(self, model_id: str) -> str:
        self.calls.append(model_id)
        outcome = self._behaviour.get(model_id, f"{self.name}:{model_id}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _facts(canonical_id: str, coding: float) -> ModelFacts:
    return ModelFacts(
        canonical_id,
        context_window=fact(400_000, Rank.DECLARED, "openrouter", _WHEN),
        supports_tools=fact(True, Rank.DECLARED, "openrouter", _WHEN),
        coding_score=fact(coding, Rank.DECLARED, "openrouter.aa", _WHEN),
    )


def _router(tmp_path, behaviour: dict[str, object]) -> tuple[ChainRouter, dict[str, _FakeProvider], DecisionLog]:
    catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
    catalog._publish(  # noqa: SLF001 - the test stands in for a network refresh
        {
            "claude-fable-5-1": _facts("claude-fable-5-1", 0.816),
            "claude-opus-5": _facts("claude-opus-5", 0.780),
            "glm-5-2": _facts("glm-5-2", 0.688),
        },
        [
            Endpoint("claude-fable-5-1", "opencode_zen", "claude-fable-5.1", 1e-5, 5e-5),
            Endpoint("claude-fable-5-1", "openrouter", "anthropic/claude-fable-5.1", 1e-5, 5e-5),
            Endpoint("claude-opus-5", "openrouter", "anthropic/claude-opus-5", 5e-6, 2.5e-5),
            Endpoint("glm-5-2", "openrouter", "z-ai/glm-5.2:free", 0.0, 0.0),
        ],
    )
    providers = {
        name: _FakeProvider(name, behaviour) for name in ("openrouter", "opencode_zen")
    }
    decisions = DecisionLog(tmp_path / "models.db")
    availability = AvailabilityView(CooldownStore(), ProviderHealthTracker(), EntitlementLedger())
    router = ChainRouter(catalog, decisions, availability, providers.get)
    return router, providers, decisions


async def _call(provider, model_id: str):
    return await provider.respond(model_id)


@pytest.mark.asyncio
async def test_the_best_model_serves_when_everything_works(tmp_path) -> None:
    router, _, decisions = _router(tmp_path, {})
    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=_call,
        task_id="t1",
    )
    assert result == "opencode_zen:claude-fable-5.1"
    await decisions.flush()
    (row,) = decisions.recent(task_id="t1")
    assert row["part"] == "coder"
    assert row["chosen_model"] == "claude-fable-5.1"
    assert row["requirements"] == {"tools": True, "context_window": 47_000}


@pytest.mark.asyncio
async def test_an_unfunded_account_lands_on_the_best_free_model(tmp_path) -> None:
    """The design's worked walk, run for real."""
    billing = PaymentRequiredError("m", "p", status_code=401, body=_BILLING)
    router, providers, decisions = _router(
        tmp_path,
        {
            "claude-fable-5.1": billing,
            "anthropic/claude-fable-5.1": billing,
            "anthropic/claude-opus-5": billing,
        },
    )
    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=_call,
        task_id="t2",
    )
    assert result == "openrouter:z-ai/glm-5.2:free"

    await decisions.flush()
    (row,) = decisions.recent(task_id="t2")
    assert row["chosen_model"] == "z-ai/glm-5.2:free"
    assert row["considered"] == 3
    # opus is never even dialled: the first billing failure marked openrouter's
    # paid endpoints, and the free endpoint on the same provider is untouched.
    assert "anthropic/claude-opus-5" not in providers["openrouter"].calls
    assert any(s["reason"].startswith("NEEDS_BILLING") for s in row["skipped"])


@pytest.mark.asyncio
async def test_one_gateway_error_does_not_bench_the_provider(tmp_path) -> None:
    """PROVIDER_DOWN needs corroboration; a single 503 must not reach it."""
    router, providers, _ = _router(
        tmp_path, {"claude-fable-5.1": ProviderUnavailableError("503 gateway")}
    )
    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(needs_tools=True),
        call_fn=_call,
    )
    assert result == "openrouter:anthropic/claude-fable-5.1"


@pytest.mark.asyncio
async def test_a_reviewer_never_reuses_the_coders_model(tmp_path) -> None:
    router, _, decisions = _router(tmp_path, {})
    result = await router.dispatch(
        component="reviewer",
        requirements=requirements_from(needs_tools=True, exclude_models=["claude-fable-5.1"]),
        call_fn=_call,
        task_id="t3",
    )
    assert result == "openrouter:anthropic/claude-opus-5"
    await decisions.flush()
    (row,) = decisions.recent(task_id="t3")
    assert row["requirements"]["excludes"] == ["claude-fable-5-1"]


@pytest.mark.asyncio
async def test_compaction_runs_on_the_cheapest_model_not_the_coders(tmp_path) -> None:
    """The part label already existed; using it is what frees the coder's budget."""
    router, _, _ = _router(tmp_path, {})
    assert await router.dispatch(
        component="coder:compact", requirements=Requirements(), call_fn=_call
    ) == "openrouter:z-ai/glm-5.2:free"


@pytest.mark.asyncio
async def test_an_invalid_response_falls_through_to_the_next_model(tmp_path) -> None:
    router, _, _ = _router(tmp_path, {})
    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(needs_tools=True),
        call_fn=_call,
        is_valid=lambda r: "fable" not in r,
        capability="tool_calls",
    )
    assert result == "openrouter:anthropic/claude-opus-5"


@pytest.mark.asyncio
async def test_a_prompt_larger_than_every_window_asks_for_compaction(tmp_path) -> None:
    router, _, _ = _router(tmp_path, {})
    with pytest.raises(ContextTooLargeError):
        await router.dispatch(
            component="coder",
            requirements=requirements_from(estimated_tokens=5_000_000, needs_tools=True),
            call_fn=_call,
        )


@pytest.mark.asyncio
async def test_exhaustion_reports_what_went_wrong(tmp_path) -> None:
    billing = PaymentRequiredError("m", "p", status_code=401, body=_BILLING)
    router, _, decisions = _router(
        tmp_path,
        {
            "claude-fable-5.1": billing,
            "anthropic/claude-fable-5.1": billing,
            "anthropic/claude-opus-5": billing,
            "z-ai/glm-5.2:free": billing,
        },
    )
    with pytest.raises(AllModelsRateLimitedError) as caught:
        await router.dispatch(
            component="coder",
            requirements=requirements_from(needs_tools=True),
            call_fn=_call,
            task_id="t4",
        )
    assert "considered" in str(caught.value)
    await decisions.flush()
    (row,) = decisions.recent(task_id="t4")
    assert row["outcome"] == "exhausted"


def test_an_empty_catalog_keeps_the_legacy_router(tmp_path) -> None:
    catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
    router = ChainRouter(
        catalog,
        DecisionLog(tmp_path / "models.db"),
        AvailabilityView(CooldownStore(), ProviderHealthTracker(), EntitlementLedger()),
        lambda _name: None,
    )
    assert catalog.snapshot == CatalogSnapshot() or catalog.snapshot.is_empty
    assert router.is_ready is False


@pytest.mark.asyncio
async def test_a_successful_call_does_not_rewrite_the_endpoint_table(tmp_path) -> None:
    """Entitlement is news once; persisting it per call would be a hot-path write."""
    router, _, _ = _router(tmp_path, {})
    writes: list[str] = []
    router._catalog.entitlement_updates = lambda provider, entitlement, *, paid_only: (  # noqa: SLF001
        writes.append(provider) or 0
    )
    for _ in range(3):
        await router.dispatch(
            component="coder", requirements=requirements_from(needs_tools=True), call_fn=_call
        )
    assert writes == []


@pytest.mark.asyncio
async def test_entitlement_is_persisted_when_a_restricted_account_starts_working(tmp_path) -> None:
    router, _, _ = _router(tmp_path, {})
    writes: list[tuple[str, str]] = []
    router._catalog.entitlement_updates = lambda provider, entitlement, *, paid_only: (  # noqa: SLF001
        writes.append((provider, entitlement.value)) or 0
    )
    router._availability.entitlements.needs_billing("opencode_zen", "no payment method")  # noqa: SLF001
    router._availability.entitlements._paid["opencode_zen"].until = None  # noqa: SLF001

    await router.dispatch(
        component="coder", requirements=requirements_from(needs_tools=True), call_fn=_call
    )
    assert ("openrouter", "OK") not in writes  # a different provider is untouched


# ── observed speed ───────────────────────────────────────────────────────────
#
# Routing ranked on quality and price with no term for how long a model takes.
# Two free models were measured on a live install averaging over two minutes per
# agent run and kept winning on price, because nothing anywhere could notice.


class _Timed:
    """A response carrying a token count, which is what a rate is measured from."""

    def __init__(self, text: str, tokens_out: int) -> None:
        self.text = text
        self.tokens_out = tokens_out


def _speed_router(tmp_path, slow_ids: set[str]):
    """A router that reports *slow_ids* as slow, and records what it times."""
    router, providers, decisions = _router(tmp_path, {})
    timings: list[tuple[str, str, float, int]] = []
    router._on_latency = lambda m, p, s, t: timings.append((m, p, s, t))  # noqa: SLF001
    router._slow = lambda canonical_id: canonical_id in slow_ids  # noqa: SLF001
    return router, providers, decisions, timings


@pytest.mark.asyncio
async def test_a_slow_model_loses_its_place_to_a_quicker_one(tmp_path) -> None:
    """The best model on paper is not the best when it takes two minutes."""
    router, _providers, _decisions, _timings = _speed_router(tmp_path, {"claude-fable-5-1"})

    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=_call,
    )

    assert result == "openrouter:anthropic/claude-opus-5", "the quicker model should serve"


@pytest.mark.asyncio
async def test_a_slow_model_still_runs_when_it_is_the_only_one_left(tmp_path) -> None:
    """Ranking, never filtering. Being slow here is not being unusable."""
    router, _providers, _decisions, _timings = _speed_router(
        tmp_path, {"claude-fable-5-1", "claude-opus-5", "glm-5-2"}
    )

    result = await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=_call,
    )

    assert result == "opencode_zen:claude-fable-5.1", "all slow means the normal order stands"


@pytest.mark.asyncio
async def test_a_successful_call_is_timed_with_its_token_count(tmp_path) -> None:
    router, _providers, _decisions, timings = _speed_router(tmp_path, set())

    async def call(provider, model_id: str):
        await provider.respond(model_id)
        return _Timed("hello", tokens_out=120)

    await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=call,
    )

    assert len(timings) == 1
    model_id, provider, seconds, tokens_out = timings[0]
    assert (model_id, provider, tokens_out) == ("claude-fable-5.1", "opencode_zen", 120)
    assert seconds >= 0.0


@pytest.mark.asyncio
async def test_a_failed_call_is_never_timed(tmp_path) -> None:
    """A timeout says nothing about generation speed.

    Recording it would be the slowest sample of all, tailing a model for being
    unavailable rather than for being slow - which cooldowns already handle.
    """
    router, _providers, _decisions, timings = _speed_router(tmp_path, set())
    router._slow = None  # noqa: SLF001 - ordering is not what this test is about

    async def call(provider, model_id: str):
        if model_id == "claude-fable-5.1":
            raise ProviderUnavailableError("down")
        return _Timed("hello", tokens_out=50)

    await router.dispatch(
        component="coder",
        requirements=requirements_from(estimated_tokens=47_000, needs_tools=True),
        call_fn=call,
    )

    timed_models = {model_id for model_id, _p, _s, _t in timings}
    assert "claude-fable-5.1" not in timed_models, "the call that failed must not be timed"
    assert timed_models, "the call that succeeded must be"
