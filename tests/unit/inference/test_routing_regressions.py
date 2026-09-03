"""Regressions for defects found auditing the chain router.

Each test names the failure it prevents, because every one of these shipped once
and none of them was caught by a test that asserted the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.cooldowns import CooldownStore
from inference.decisions import DecisionLog
from inference.exceptions import AllModelsRateLimitedError, ContextTooLargeError
from inference.facts.catalog import FactsCatalog
from inference.facts.models import Endpoint, ModelFacts, Rank, fact
from inference.facts.store import ModelFactsStore
from inference.provider_health import ProviderHealthTracker
from inference.routing.availability import AvailabilityView, EntitlementLedger
from inference.routing.chain import ChainWalk, Requirements, Skip
from inference.routing.parts import profile_for, with_pool
from inference.routing.router import ChainRouter, requirements_from

_WHEN = datetime(2026, 9, 3, tzinfo=UTC)


def _facts(canonical_id: str, score: float = 0.5, *, ctx: int = 400_000, completion: bool = True) -> ModelFacts:
    return ModelFacts(
        canonical_id,
        context_window=fact(ctx, Rank.DECLARED, "openrouter", _WHEN),
        supports_completion=fact(completion, Rank.DECLARED, "openrouter", _WHEN),
        supports_tools=fact(True, Rank.DECLARED, "openrouter", _WHEN),
        supports_structured=fact(True, Rank.DECLARED, "openrouter", _WHEN),
        coding_score=fact(score, Rank.DECLARED, "openrouter.aa", _WHEN),
        intelligence_score=fact(score, Rank.DECLARED, "openrouter.aa", _WHEN),
    )


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def respond(self, model_id: str) -> str:
        return f"{self.name}:{model_id}"


async def _call(provider: _Provider, model_id: str) -> str:
    return await provider.respond(model_id)


def _router(tmp_path, facts, endpoints, wired=("openrouter",)):
    catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
    catalog._publish(facts, endpoints)  # noqa: SLF001 - stands in for a network refresh
    availability = AvailabilityView(CooldownStore(), ProviderHealthTracker(), EntitlementLedger())
    providers = {name: _Provider(name) for name in wired}
    return ChainRouter(catalog, DecisionLog(tmp_path / "models.db"), availability, providers.get), catalog


@pytest.mark.asyncio
async def test_a_transcription_model_can_never_serve_a_completion(tmp_path) -> None:
    """A chain only serves completions, whatever a part does or does not require.

    Embedding, transcription, image and moderation models live in the same
    catalogs and registries as chat models and are far cheaper. With no floor,
    a cheapest-first part picked Whisper on any install without free chat models.
    """
    router, _ = _router(
        tmp_path,
        {"chat": _facts("chat", 0.5), "whisper": _facts("whisper", 0.9, completion=False)},
        [
            Endpoint("chat", "openrouter", "chat", 1e-5, 1e-5),
            Endpoint("whisper", "openrouter", "whisper-large-v3", 1e-9, 1e-9),
        ],
    )
    served = await router.dispatch(component="coder:compact", requirements=Requirements(), call_fn=_call)
    assert served == "openrouter:chat"


@pytest.mark.asyncio
async def test_an_unconfigured_provider_does_not_disqualify_the_model(tmp_path) -> None:
    """A model is not at fault for being listed against a key the user removed."""
    router, _ = _router(
        tmp_path,
        {"best": _facts("best", 0.9), "worse": _facts("worse", 0.1)},
        [
            Endpoint("best", "opencode_zen", "best", 0.0, 0.0),  # key since removed
            Endpoint("best", "openrouter", "vendor/best", 1e-5, 1e-5),
            Endpoint("worse", "openrouter", "vendor/worse", 1e-5, 1e-5),
        ],
    )
    assert await router.dispatch(
        component="coder", requirements=requirements_from(needs_tools=True), call_fn=_call
    ) == "openrouter:vendor/best"


class TestObservedContradiction:
    """Observation contradicts a declaration - but only with corroboration."""

    def _catalog(self, tmp_path) -> FactsCatalog:
        catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
        catalog._publish(  # noqa: SLF001
            {"m": _facts("m", 0.9)},
            [Endpoint("m", "openrouter", "m", 1e-5, 1e-5), Endpoint("m", "opencode_zen", "m", 1e-5, 1e-5)],
        )
        return catalog

    def test_one_failure_is_noise(self, tmp_path) -> None:
        catalog = self._catalog(tmp_path)
        assert catalog.contradict("m", "supports_tools", "openrouter") is False
        assert catalog.snapshot.facts["m"].value("supports_tools") is True

    def test_the_same_endpoint_failing_twice_is_still_one_witness(self, tmp_path) -> None:
        catalog = self._catalog(tmp_path)
        catalog.contradict("m", "supports_tools", "openrouter")
        assert catalog.contradict("m", "supports_tools", "openrouter") is False

    def test_two_independent_failures_demote_the_fact(self, tmp_path) -> None:
        catalog = self._catalog(tmp_path)
        catalog.contradict("m", "supports_tools", "openrouter")
        assert catalog.contradict("m", "supports_tools", "opencode_zen") is True
        record = catalog.snapshot.facts["m"]
        assert record.value("supports_tools") is False
        assert record.get("supports_tools").rank is Rank.OBSERVED

    def test_a_demoted_model_leaves_the_chain(self, tmp_path) -> None:
        catalog = self._catalog(tmp_path)
        catalog.contradict("m", "supports_tools", "openrouter")
        catalog.contradict("m", "supports_tools", "opencode_zen")
        assert not [c for c in catalog.chain_for(profile_for("coder")) if c.canonical_id == "m"]

    def test_observation_cannot_invent_a_capability(self, tmp_path) -> None:
        """It only ever turns a declared true into false, never the reverse."""
        catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
        catalog._publish({"m": ModelFacts("m")}, [Endpoint("m", "openrouter", "m", 0.0, 0.0)])  # noqa: SLF001
        assert catalog.contradict("m", "supports_tools", "openrouter") is False
        assert catalog.snapshot.facts["m"].get("supports_tools") is None

    @pytest.mark.asyncio
    async def test_a_refresh_cannot_undo_an_observation(self, tmp_path) -> None:
        """The sources re-declare the same thing every refresh; the observation stands."""
        catalog = self._catalog(tmp_path)
        catalog.contradict("m", "supports_tools", "openrouter")
        catalog.contradict("m", "supports_tools", "opencode_zen")
        await catalog.refresh(
            [],
            {
                "openrouter": {
                    "m": ModelInfo(
                        "m",
                        "openrouter",
                        frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
                        400_000,
                        1e-5,
                        0.5,
                    )
                }
            },
            configured_providers=["openrouter"],
        )
        assert catalog.snapshot.facts["m"].value("supports_tools") is False


class TestProviderRows:
    _CAPS = frozenset({ModelCapability.COMPLETION})

    def _models(self, provider: str, model_id: str) -> dict[str, ModelInfo]:
        return {model_id: ModelInfo(model_id, provider, self._CAPS, 128_000, 0.0, 0.5)}

    @pytest.mark.asyncio
    async def test_removing_a_key_drops_that_providers_endpoints(self, tmp_path) -> None:
        """Otherwise its rows persist forever and every model they serve is skipped."""
        catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
        both = {"opencode_zen": self._models("opencode_zen", "z1"), "openrouter": self._models("openrouter", "o1")}
        await catalog.refresh([], both, configured_providers=["opencode_zen", "openrouter"])
        await catalog.refresh([], {"openrouter": both["openrouter"]}, configured_providers=["openrouter"])
        providers = {e.provider for group in catalog.snapshot.endpoints_by_model.values() for e in group}
        assert providers == {"openrouter"}

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_its_endpoints(self, tmp_path) -> None:
        """Stale facts beat no facts - the whole reason they are persisted."""
        catalog = FactsCatalog(ModelFactsStore(tmp_path / "models.db"))
        await catalog.refresh([], {"openrouter": self._models("openrouter", "o1")}, configured_providers=["openrouter"])
        await catalog.refresh([], {}, configured_providers=["openrouter"])
        providers = {e.provider for group in catalog.snapshot.endpoints_by_model.values() for e in group}
        assert providers == {"openrouter"}


class TestContextVersusPayload:
    """Compaction fixes one of these and is useless against the other."""

    @pytest.mark.asyncio
    async def test_a_payload_cap_is_not_reported_as_a_context_overflow(self, tmp_path) -> None:
        router, _ = _router(
            tmp_path,
            {"tiny": _facts("tiny", 0.5, ctx=1_000_000)},
            [Endpoint("tiny", "openrouter", "tiny", 0.0, 0.0, max_payload_chars=1_000)],
        )
        with pytest.raises(AllModelsRateLimitedError) as caught:
            await router.dispatch(
                component="coder",
                requirements=requirements_from(estimated_tokens=100, payload_chars=999_999, needs_tools=True),
                call_fn=_call,
            )
        assert "payload" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_real_overflow_still_asks_for_compaction(self, tmp_path) -> None:
        router, _ = _router(
            tmp_path,
            {"tiny": _facts("tiny", 0.5, ctx=200_000)},
            [Endpoint("tiny", "openrouter", "tiny", 0.0, 0.0)],
        )
        with pytest.raises(ContextTooLargeError):
            await router.dispatch(
                component="coder",
                requirements=requirements_from(estimated_tokens=9_000_000, needs_tools=True),
                call_fn=_call,
            )


class TestModelPool:
    """`model_pool` is still in every agent config and in the create_agent tool."""

    @pytest.mark.asyncio
    async def test_fast_cheap_moves_a_part_off_its_quality_ranking(self, tmp_path) -> None:
        router, _ = _router(
            tmp_path,
            {"cheap": _facts("cheap", 0.2), "dear": _facts("dear", 0.9)},
            [Endpoint("cheap", "openrouter", "cheap", 1e-9, 1e-9), Endpoint("dear", "openrouter", "dear", 1e-5, 1e-5)],
        )
        needs = requirements_from(needs_tools=True)
        assert await router.dispatch(component="coder", requirements=needs, call_fn=_call) == "openrouter:dear"
        assert (
            await router.dispatch(component="coder", requirements=needs, call_fn=_call, pool="fast_cheap")
            == "openrouter:cheap"
        )

    def test_the_default_pool_never_changes_which_quality_axis_a_part_uses(self) -> None:
        """`reasoning` is the default in every agent config; it must be a no-op here."""
        assert with_pool(profile_for("coder"), "reasoning").order_by == "coding_score"

    def test_reasoning_lifts_a_cheapest_first_part(self) -> None:
        assert with_pool(profile_for("planner"), "reasoning").order_by == "intelligence_score"


class TestRetryHint:
    def test_retry_after_is_data_not_a_parsed_display_string(self) -> None:
        walk = ChainWalk([], None)
        walk.skipped = [Skip("m", "p", "cooling down (42s)", 42.0), Skip("m2", "p", "NEEDS_BILLING")]
        assert walk.soonest_retry() == 42.0

    def test_a_reason_with_no_timing_cannot_crash_exhaustion(self) -> None:
        walk = ChainWalk([], None)
        walk.skipped = [Skip("m", "p", "provider not configured")]
        assert walk.soonest_retry() is None


def test_an_endpoint_is_free_only_when_a_price_says_so() -> None:
    assert Endpoint("m", "p", "m", price_in=0.0, price_out=None).is_free is True
    assert Endpoint("m", "p", "m").is_free is False  # unknown price is not free
    assert Endpoint("m", "p", "m", price_in=1e-6, price_out=1e-5).is_free is False


def test_a_models_db_from_an_older_north_keeps_its_rows(tmp_path) -> None:
    """New fact fields are added as nullable columns, never by rebuilding the table."""
    import sqlite3

    db = tmp_path / "models.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE model_facts (canonical_id TEXT PRIMARY KEY, context_window INTEGER,"
            " provenance TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO model_facts VALUES ('old', 200000, '{}', '2026-09-01T00:00:00+00:00')")

    loaded = ModelFactsStore(db).load_facts()
    assert loaded["old"].value("context_window") == 200_000
    assert loaded["old"].value("supports_completion") is None
