"""One server error no longer fails the call (issue #82, item 5).

Three chat turns failed with "No model could serve north - 1 models / 1 endpoints:
1 gateway or server outage". A 502 set no cooldown, so the router had no wait to
wait out and failed at once; a 500 set a fixed 60 s cooldown, longer than the
router's 40 s wait limit. Now a failure that names no wait cools its model for
0.5 s, then twice as long each time in a row, and the router walks again while
the waits fit in its budget, as the Anthropic SDK retries.
"""

from __future__ import annotations

import pytest

from inference import cooldowns as cooldowns_module
from inference.capability import ModelInfo
from inference.cooldowns import CooldownStore
from inference.dispatcher import ModelDispatcher
from inference.exceptions import (
    AllModelsRateLimitedError,
    InferenceError,
    ModelRateLimitedError,
    PaymentRequiredError,
    ProviderUnavailableError,
)
from inference.models import CompletionRequest, CompletionResponse, TranscriptionRequest, TranscriptionResponse
from tests.unit.inference._catalog import publish_catalog

_KEY = ("m", "p")


def _cooldown(store: CooldownStore) -> float:
    return round(store.remaining(_KEY), 1)


# ---- the cooldown store ----


def test_a_failure_with_no_wait_named_cools_for_half_a_second_then_doubles() -> None:
    store = CooldownStore()
    seen = []
    for _ in range(9):
        store.set_rate_limit(_KEY)
        seen.append(_cooldown(store))

    assert seen == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_a_wait_the_provider_named_is_used_as_it_is_and_does_not_count() -> None:
    store = CooldownStore()
    store.set_rate_limit(_KEY, seconds=7)
    assert _cooldown(store) == 7.0

    store.set_rate_limit(_KEY)
    assert _cooldown(store) == 0.5


def test_an_answer_starts_the_doubling_again() -> None:
    store = CooldownStore()
    for _ in range(4):
        store.set_rate_limit(_KEY)

    store.forget_failures(_KEY)
    store.set_rate_limit(_KEY)

    assert _cooldown(store) == 0.5


def test_a_model_quiet_for_five_minutes_starts_again(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(cooldowns_module.time, "monotonic", lambda: clock[0])
    store = CooldownStore()
    for _ in range(4):
        store.set_rate_limit(_KEY)

    clock[0] += 301
    store.set_rate_limit(_KEY)

    assert store.remaining(_KEY) == 0.5


# ---- the router, end to end ----


class _Flaky:
    """One model that fails *failures* times with *error*, then answers."""

    def __init__(self, error, failures: int, name: str = "codex") -> None:
        self.name = name
        self.error = error
        self.failures = failures
        self.calls = 0

    def get_models(self) -> dict[str, ModelInfo]:
        return {
            "only-model": ModelInfo(
                model_id="only-model",
                provider_name=self.name,
                capabilities=frozenset(["completion"]),
                context_window=128_000,
                cost_per_token=0.0,
                base_quality=0.8,
            )
        }

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error(model_id)
        return CompletionResponse(text="answered", model_used=model_id, tokens_in=1, tokens_out=1, cost_usd=0.0)


def _dispatcher(provider, tmp_path) -> ModelDispatcher:
    dispatcher = ModelDispatcher(
        [provider], cooldowns_path=tmp_path / "cooldowns.json", models_db_path=tmp_path / "models.db"
    )
    publish_catalog(dispatcher)
    return dispatcher


@pytest.fixture
def quick_waits(monkeypatch):
    """The same waits and doubling, a hundred times shorter, so a test does not sleep for seconds."""
    monkeypatch.setattr("inference.cooldowns._FIRST_COOLDOWN_SECS", 0.005)
    monkeypatch.setattr("inference.routing.router._WAIT_GRACE_SECONDS", 0.005)


def _outage(model_id):
    return ProviderUnavailableError("OpenAI Codex returned 502")


def _server_error(model_id):
    return InferenceError("OpenAI Codex returned 500: internal error")


@pytest.mark.parametrize("error", [_outage, _server_error], ids=["502", "500"])
async def test_a_chain_of_one_model_waits_out_server_errors(tmp_path, quick_waits, error) -> None:
    provider = _Flaky(error, failures=3)

    response = await _dispatcher(provider, tmp_path).complete(CompletionRequest(prompt="hi", component="north"))

    assert response.text == "answered"
    assert provider.calls == 4


async def test_an_outage_longer_than_the_budget_fails_with_a_wait_the_agent_can_use(tmp_path, quick_waits) -> None:
    provider = _Flaky(_outage, failures=100)

    with pytest.raises(AllModelsRateLimitedError) as caught:
        await _dispatcher(provider, tmp_path).complete(CompletionRequest(prompt="hi", component="north"))

    assert caught.value.retry_after is not None, "the agent layer waits out a failure that names a wait"
    assert 1 < provider.calls <= 8


async def test_a_billing_failure_is_not_waited_for(tmp_path, quick_waits) -> None:
    provider = _Flaky(lambda model_id: PaymentRequiredError(model_id, "codex", status_code=402), failures=100)

    with pytest.raises(AllModelsRateLimitedError) as caught:
        await _dispatcher(provider, tmp_path).complete(CompletionRequest(prompt="hi", component="north"))

    assert provider.calls == 1
    assert caught.value.retry_after is None


async def test_a_tiny_wait_named_each_time_cannot_become_dozens_of_requests(tmp_path, quick_waits) -> None:
    provider = _Flaky(lambda model_id: ModelRateLimitedError(model_id, "codex", retry_after=0.001), failures=100)

    with pytest.raises(AllModelsRateLimitedError):
        await _dispatcher(provider, tmp_path).complete(CompletionRequest(prompt="hi", component="north"))

    assert provider.calls == 8


# ---- embeddings and transcription: the same rule ----


class _FlakyTranscriber:
    """One transcription model that fails *failures* times with *error*, then answers."""

    name = "codex"

    def __init__(self, error, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0

    def get_models(self) -> dict[str, ModelInfo]:
        return {
            "whisper": ModelInfo(
                model_id="whisper",
                provider_name=self.name,
                capabilities=frozenset(["transcription"]),
                context_window=0,
                cost_per_token=0.0,
                base_quality=0.8,
            )
        }

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error(model_id)
        return TranscriptionResponse(text="heard", model_used=model_id)


@pytest.fixture
def quick_dispatcher_waits(monkeypatch):
    monkeypatch.setattr("inference.cooldowns._FIRST_COOLDOWN_SECS", 0.005)
    monkeypatch.setattr("inference.dispatcher._WAIT_GRACE_SECONDS", 0.005)


@pytest.mark.parametrize("error", [_outage, _server_error], ids=["502", "500"])
async def test_transcription_waits_out_server_errors(tmp_path, quick_dispatcher_waits, error) -> None:
    provider = _FlakyTranscriber(error, failures=3)
    dispatcher = ModelDispatcher([provider], cooldowns_path=tmp_path / "c.json", models_db_path=tmp_path / "m.db")

    response = await dispatcher.transcribe(TranscriptionRequest(audio=b"x"))

    assert response.text == "heard"
    assert provider.calls == 4


async def test_a_cooldown_that_ended_before_the_check_is_tried_again(tmp_path, monkeypatch) -> None:
    """On a slow machine the short cooldown can end before the dispatcher asks how long to wait.

    The dispatcher read "no wait left" as "nothing to wait for" and failed the call, although the model
    was ready. A first cooldown of 0 s makes that order certain. CI saw it once with a 5 ms cooldown.
    """
    monkeypatch.setattr("inference.cooldowns._FIRST_COOLDOWN_SECS", 0.0)
    provider = _FlakyTranscriber(_outage, failures=2)
    dispatcher = ModelDispatcher([provider], cooldowns_path=tmp_path / "c.json", models_db_path=tmp_path / "m.db")

    response = await dispatcher.transcribe(TranscriptionRequest(audio=b"x"))

    assert response.text == "heard"
    assert provider.calls == 3


async def test_two_server_errors_do_not_close_the_provider_for_every_call(tmp_path, quick_dispatcher_waits) -> None:
    """The breaker is shared with chat routing; it used to open after two 502s on one transcription model."""
    provider = _FlakyTranscriber(_outage, failures=2)
    dispatcher = ModelDispatcher([provider], cooldowns_path=tmp_path / "c.json", models_db_path=tmp_path / "m.db")

    await dispatcher.transcribe(TranscriptionRequest(audio=b"x"))

    assert dispatcher._provider_health.is_available("codex")
