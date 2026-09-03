"""Failure scope: a failure must implicate only what the evidence supports."""

from __future__ import annotations

import pytest

from inference.exceptions import (
    ContextTooLargeError,
    InferenceError,
    ModelNotFoundError,
    ModelRateLimitedError,
    PayloadTooLargeError,
    PaymentRequiredError,
    ProviderAuthError,
    ProviderUnavailableError,
)
from inference.failure import OutageCorroboration, Scope, classify

_BILLING_BODY = {"error": {"type": "CreditsError", "message": "No payment method on file"}}


def test_the_401_that_started_this_is_about_the_account_not_the_provider() -> None:
    """Zen bills through 401. Read as auth, one reply lost 66 models for a day."""
    failure = classify(
        PaymentRequiredError("claude-opus-4-8", "opencode_zen", status_code=401, body=_BILLING_BODY),
    )
    assert failure.scope is Scope.ACCOUNT_PAID
    assert not failure.is_provider_wide


def test_a_401_without_a_billing_marker_is_a_bad_key() -> None:
    failure = classify(ProviderAuthError("opencode_zen returned 401 - provider auth failed"))
    assert failure.scope is Scope.PROVIDER_AUTH


def test_a_billing_message_on_an_auth_error_is_still_about_money() -> None:
    failure = classify(ProviderAuthError("401: your credits are exhausted"))
    assert failure.scope is Scope.ACCOUNT_PAID


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ModelRateLimitedError("m", "p", retry_after=12), Scope.MODEL),
        (ModelNotFoundError("m", "p"), Scope.MODEL),
        (PayloadTooLargeError("m", "p"), Scope.MODEL),
        (ProviderUnavailableError("503 gateway"), Scope.PROVIDER_DOWN),
        (ContextTooLargeError(500_000, 200_000), Scope.REQUEST),
    ],
)
def test_signal_maps_to_scope(exc: Exception, expected: Scope) -> None:
    assert classify(exc, model_id="m", provider="p").scope is expected


def test_a_rate_limit_with_no_reset_and_credits_language_is_an_entitlement_fact() -> None:
    """Gemini reports credit exhaustion as a 429 with no reset window."""
    exc = ModelRateLimitedError("m", "p", retry_after=None, body={"error": {"message": "credits depleted"}})
    assert classify(exc).scope is Scope.ACCOUNT_PAID


def test_a_400_rejecting_tools_contradicts_a_capability() -> None:
    exc = InferenceError("provider returned 400: this model does not support tools")
    assert classify(exc, capability="tool_calls").scope is Scope.MODEL_CAPABILITY


def test_a_retry_after_travels_with_the_failure() -> None:
    assert classify(ModelRateLimitedError("m", "p", retry_after=30)).retry_after == 30


class TestOutageCorroboration:
    def test_one_call_can_never_declare_a_provider_down(self) -> None:
        votes = OutageCorroboration(quorum=3)
        assert votes.record("openrouter", "model-a") is False

    def test_distinct_models_are_required_not_repeats(self) -> None:
        votes = OutageCorroboration(quorum=3)
        for _ in range(5):
            assert votes.record("openrouter", "model-a") is False

    def test_a_quorum_of_distinct_models_corroborates(self) -> None:
        votes = OutageCorroboration(quorum=3)
        assert [votes.record("openrouter", m) for m in ("a", "b", "c")] == [False, False, True]

    def test_a_success_clears_the_votes(self) -> None:
        votes = OutageCorroboration(quorum=2)
        votes.record("openrouter", "a")
        votes.clear("openrouter")
        assert votes.record("openrouter", "b") is False
