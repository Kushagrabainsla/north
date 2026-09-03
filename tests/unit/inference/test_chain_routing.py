"""Chain construction and the walk.

Two properties are asserted directly because they encode the bugs this replaces:
a billing failure must never reduce the free-tier chain, and the reviewer's chain
must never contain the coder's model for that task.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inference.cooldowns import CooldownStore
from inference.decisions import DecisionLog, RoutingDecision
from inference.facts.merge import ScorePrior
from inference.facts.models import Endpoint, ModelFacts, Rank, fact
from inference.failure import Failure, Scope
from inference.provider_health import ProviderHealthTracker
from inference.routing.availability import AvailabilityView, EntitlementLedger
from inference.routing.chain import ChainWalk, Requirements, build_chain, narrow
from inference.routing.parts import Order, PartProfile, profile_for, with_power

_WHEN = datetime(2026, 9, 3, tzinfo=UTC)


def _facts(canonical_id: str, coding: float | None = None, *, tools: bool = True, ctx: int = 400_000):
    measured = None if coding is None else fact(coding, Rank.DECLARED, "openrouter.aa", _WHEN)
    return ModelFacts(
        canonical_id,
        context_window=fact(ctx, Rank.DECLARED, "openrouter", _WHEN),
        supports_tools=fact(tools, Rank.DECLARED, "openrouter", _WHEN),
        supports_structured=fact(True, Rank.DECLARED, "openrouter", _WHEN),
        coding_score=measured,
        # The two indices move together in the real catalog, which is what lets a
        # part ranked on one be compared against a floor on the other.
        intelligence_score=measured,
    )


def _catalog():
    """A miniature of the real thing: two paid leaders and a free tail."""
    facts = {
        "claude-fable-5-1": _facts("claude-fable-5-1", 0.816),
        "claude-opus-5": _facts("claude-opus-5", 0.780),
        "glm-5-2": _facts("glm-5-2", 0.688),
        "minimax-m3": _facts("minimax-m3", 0.586),
        "no-tools-model": _facts("no-tools-model", 0.900, tools=False),
    }
    endpoints = {
        "claude-fable-5-1": [
            Endpoint("claude-fable-5-1", "opencode_zen", "claude-fable-5.1", 1e-5, 5e-5),
            Endpoint("claude-fable-5-1", "openrouter", "anthropic/claude-fable-5.1", 1e-5, 5e-5),
        ],
        "claude-opus-5": [Endpoint("claude-opus-5", "openrouter", "anthropic/claude-opus-5", 5e-6, 2.5e-5)],
        "glm-5-2": [Endpoint("glm-5-2", "openrouter", "z-ai/glm-5.2:free", 0.0, 0.0)],
        "minimax-m3": [Endpoint("minimax-m3", "openrouter", "minimax/minimax-m3:free", 0.0, 0.0)],
        "no-tools-model": [Endpoint("no-tools-model", "openrouter", "vendor/no-tools", 1e-6, 1e-6)],
    }
    return facts, endpoints


def _chain(profile: PartProfile | None = None, **kwargs):
    facts, endpoints = _catalog()
    profile = profile or profile_for("coder")
    return build_chain(
        profile,
        Requirements(capabilities=profile.requires, min_context=profile.min_context),
        facts,
        endpoints,
        ScorePrior([1e-5, 2.5e-5, 0.0], [0.816, 0.780, 0.688, 0.586]),
        **kwargs,
    )


def _view(entitlements: EntitlementLedger | None = None) -> AvailabilityView:
    return AvailabilityView(CooldownStore(), ProviderHealthTracker(), entitlements or EntitlementLedger())


class TestChainConstruction:
    def test_ordered_by_measured_coding_score(self) -> None:
        assert [c.canonical_id for c in _chain()][:4] == [
            "claude-fable-5-1",
            "claude-opus-5",
            "glm-5-2",
            "minimax-m3",
        ]

    def test_the_free_tier_is_the_tail_of_the_same_list(self) -> None:
        """Not a separate fallback: there is nothing to keep in sync."""
        chain = _chain()
        assert any(c.price == 0.0 for c in chain)
        assert chain[-1].price == 0.0

    def test_a_declared_capability_excludes_even_a_strong_model(self) -> None:
        assert "no-tools-model" not in {c.canonical_id for c in _chain()}

    def test_the_order_is_deterministic(self) -> None:
        assert [c.canonical_id for c in _chain()] == [c.canonical_id for c in _chain()]

    def test_max_price_is_a_hard_stop(self) -> None:
        capped = PartProfile("coder", frozenset({"tools"}), 0, Order.CODING, max_price=1e-6)
        assert {c.canonical_id for c in _chain(capped)} == {"glm-5-2", "minimax-m3"}

    def test_a_demoted_model_moves_to_the_tail_but_stays_in_the_chain(self) -> None:
        chain = _chain(demoted=lambda cid: cid == "claude-fable-5-1")
        assert chain[0].canonical_id == "claude-opus-5"
        assert chain[-1].canonical_id == "claude-fable-5-1"

    def test_a_pin_beats_the_ranking(self) -> None:
        pinned = PartProfile("coder", frozenset({"tools"}), 0, Order.CODING, pinned_model="glm-5.2")
        assert _chain(pinned)[0].canonical_id == "glm-5-2"

    def test_a_pin_that_matches_nothing_is_ignored(self) -> None:
        pinned = PartProfile("coder", frozenset({"tools"}), 0, Order.CODING, pinned_model="not-a-model")
        assert _chain(pinned)[0].canonical_id == "claude-fable-5-1"

    def test_the_reviewer_chain_never_contains_the_coders_model(self) -> None:
        """The property the two-list router violated whenever the primary ran dry."""
        chain = narrow(_chain(profile_for("reviewer")), Requirements(exclude=frozenset({"claude-fable-5-1"})))
        assert "claude-fable-5-1" not in {c.canonical_id for c in chain}
        assert chain, "excluding one model must not empty an exhaustive chain"


class TestBillingNeverReducesTheFreeTier:
    def test_a_billing_failure_leaves_every_free_endpoint_callable(self) -> None:
        view = _view()
        paid = Endpoint("claude-opus-5", "opencode_zen", "claude-opus-5", 1e-5, 2.5e-5)
        free = Endpoint("glm-5-2", "opencode_zen", "glm-5-free", 0.0, 0.0)
        view.apply(Failure(Scope.ACCOUNT_PAID, "no payment method", "claude-opus-5", "opencode_zen"), paid)
        assert view.skip_reason(paid) is not None
        assert view.skip_reason(free) is None

    def test_the_same_model_is_still_reachable_on_another_provider(self) -> None:
        view = _view()
        zen = Endpoint("claude-fable-5-1", "opencode_zen", "claude-fable-5.1", 1e-5, 5e-5)
        other = Endpoint("claude-fable-5-1", "openrouter", "anthropic/claude-fable-5.1", 1e-5, 5e-5)
        view.apply(Failure(Scope.ACCOUNT_PAID, "no payment method", "claude-fable-5.1", "opencode_zen"), zen)
        assert view.skip_reason(other) is None


class TestWalk:
    def test_an_unfunded_account_walks_down_to_the_best_free_model(self) -> None:
        """The design's worked walk: every paid endpoint needs billing."""
        ledger = EntitlementLedger()
        ledger.needs_billing("opencode_zen", "no payment method")
        ledger.needs_billing("openrouter", "no credit")
        walk = ChainWalk(_chain(), _view(ledger))

        served = None
        for attempt in walk.attempts():
            served = attempt  # the first reachable endpoint wins
            break

        assert served is not None
        assert served.candidate.canonical_id == "glm-5-2"
        assert all("NEEDS_BILLING" in skip.reason for skip in walk.skipped)

    def test_a_model_scoped_failure_moves_to_the_next_model(self) -> None:
        walk = ChainWalk(_chain(), _view())
        seen: list[str] = []
        for attempt in walk.attempts():
            seen.append(attempt.candidate.canonical_id)
            attempt.failed(Failure(Scope.MODEL, "404", attempt.model_id, attempt.provider))
        assert seen == ["claude-fable-5-1", "claude-opus-5", "glm-5-2", "minimax-m3"]

    def test_an_account_scoped_failure_retries_the_same_model_elsewhere(self) -> None:
        """The model was never the problem, so the next provider gets a turn."""
        walk = ChainWalk(_chain(), _view())
        attempts = walk.attempts()
        first = next(attempts)
        first.failed(Failure(Scope.ACCOUNT_PAID, "needs billing", first.model_id, first.provider))
        second = next(attempts)
        assert second.candidate.canonical_id == first.candidate.canonical_id
        assert second.provider != first.provider

    def test_a_request_scoped_failure_stops_the_walk(self) -> None:
        walk = ChainWalk(_chain(), _view())
        attempts = walk.attempts()
        first = next(attempts)
        first.failed(Failure(Scope.REQUEST, "prompt is malformed", first.model_id, first.provider))
        with pytest.raises(StopIteration):
            next(attempts)

    def test_exhaustion_says_why_not_just_that(self) -> None:
        """"All N candidates exhausted" is not actionable; a tally of reasons is."""
        ledger = EntitlementLedger()
        ledger.needs_billing("opencode_zen", "no payment method")
        ledger.needs_billing("openrouter", "no credit")
        walk = ChainWalk(_chain(), _view(ledger))

        for attempt in walk.attempts():
            # Only the free tail is still reachable; fail it too, so the chain
            # genuinely runs out.
            attempt.failed(Failure(Scope.MODEL, "rate limited", attempt.model_id, attempt.provider))

        summary = walk.exhaustion_summary()
        assert summary.startswith("4 considered:")
        assert "NEEDS_BILLING" in summary
        assert "rate limited" in summary


class TestDecisionLog:
    def test_a_decision_answers_why_this_model_ran(self, tmp_path) -> None:
        log = DecisionLog(tmp_path / "models.db")
        decision = RoutingDecision(
            part="coder",
            task_id="task_96a15d78a40e",
            requirements={"tools": True, "context_window": 47_000},
            considered=17,
            skipped=[{"model": "claude-fable-5.1", "provider": "zen", "reason": "NEEDS_BILLING"}],
        )
        decision.chose("z-ai/glm-5.2:free", "openrouter")
        log.record(decision)

        (row,) = log.recent(task_id="task_96a15d78a40e")
        assert row["part"] == "coder"
        assert row["chosen_model"] == "z-ai/glm-5.2:free"
        assert row["outcome"] == "success"
        assert row["considered"] == 17
        assert row["skipped"][0]["reason"] == "NEEDS_BILLING"

    def test_an_unwritable_log_never_fails_a_call(self, tmp_path) -> None:
        log = DecisionLog(tmp_path / "nested" / "models.db", enabled=False)
        log.record(RoutingDecision(part="coder", requirements={}))
        assert log.recent() == []


class TestPowerDial:
    """eco/cruise/sport predate chains but stay live controls, so they keep working."""

    def _order(self, power: str, part: str = "coder") -> list[str]:
        profile = with_power(profile_for(part), power)
        return [c.canonical_id for c in _chain(profile)]

    def test_cruise_uses_the_parts_own_profile(self) -> None:
        assert self._order("cruise")[0] == "claude-fable-5-1"

    def test_eco_orders_every_part_cheapest_first(self) -> None:
        assert self._order("eco")[0] in ("glm-5-2", "minimax-m3")

    def test_sport_orders_a_cheap_part_on_quality(self) -> None:
        """The planner is cheapest-first by default; sport ranks it on quality.

        It needs structured output rather than tools, so the strongest model that
        declares structured output leads - price no longer enters the ordering.
        """
        cheapest_first = self._order("cruise", "planner")
        assert self._order("sport", "planner")[0] == "no-tools-model"
        assert cheapest_first[0] != "no-tools-model"

    def test_the_dial_never_relaxes_a_requirement(self) -> None:
        for power in ("eco", "cruise", "sport"):
            assert "no-tools-model" not in self._order(power)
