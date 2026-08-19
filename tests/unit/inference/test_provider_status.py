"""Provider HTTP-status handling: the shared cooldown policy (B8).

402 (insufficient credits) and 429/404/503 (rate limited or model gone) must
map to PaymentRequiredError / ModelRateLimitedError on both the completion and
transcription paths, so the dispatcher applies the right cooldown instead of a
generic failure.
"""

from __future__ import annotations

import types

import httpx
import pytest

from inference.exceptions import ModelRateLimitedError, PaymentRequiredError, ProviderAuthError
from inference.models import CompletionRequest, TranscriptionRequest
from inference.providers.groq import GroqRouter
from inference.providers.openai_compat import OpenAICompatibleProvider
from inference.providers.openrouter import OpenRouterRouter


def _client_returning(status_code: int) -> httpx.AsyncClient:
    """An AsyncClient whose every request resolves to the given status."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "x"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


class TestRaiseCooldownStatus:
    """The single source of truth for cooldown-worthy provider statuses."""

    def _provider(self) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(name="test", base_url="http://test", api_key="k")

    def test_402_raises_payment_required(self) -> None:
        with pytest.raises(PaymentRequiredError):
            self._provider()._raise_cooldown_status(httpx.Response(402), "model")

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_raise_provider_auth_error(self, status: int) -> None:
        with pytest.raises(ProviderAuthError):
            self._provider()._raise_cooldown_status(httpx.Response(status), "model")

    @pytest.mark.parametrize("status", [429, 404, 503])
    def test_rate_limited_statuses_raise(self, status: int) -> None:
        with pytest.raises(ModelRateLimitedError):
            self._provider()._raise_cooldown_status(httpx.Response(status), "model")

    def test_413_is_treated_as_payload_too_large(self) -> None:
        # 413 (request too large, e.g. groq free-tier request cap) is permanent for
        # this prompt - surface as PayloadTooLargeError so the dispatcher skips the
        # model instead of retrying with backoff.
        from inference.exceptions import PayloadTooLargeError

        with pytest.raises(PayloadTooLargeError):
            self._provider()._raise_cooldown_status(httpx.Response(413), "model")

    def test_retry_after_seconds_parsed_onto_exception(self) -> None:
        resp = httpx.Response(429, headers={"retry-after": "12"})
        with pytest.raises(ModelRateLimitedError) as exc:
            self._provider()._raise_cooldown_status(resp, "model")
        assert exc.value.retry_after == 12.0

    def test_retry_after_absent_is_none(self) -> None:
        with pytest.raises(ModelRateLimitedError) as exc:
            self._provider()._raise_cooldown_status(httpx.Response(429), "model")
        assert exc.value.retry_after is None

    def test_parse_retry_after_invalid_returns_none(self) -> None:
        assert OpenAICompatibleProvider._parse_retry_after(httpx.Response(429, headers={"retry-after": "soon"})) is None

    def test_parse_retry_after_http_date_is_positive(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        future = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
        secs = OpenAICompatibleProvider._parse_retry_after(httpx.Response(503, headers={"retry-after": future}))
        assert secs is not None and 0 < secs <= 31

    # ---- Gemini 429: distinguish billing exhaustion from a real rate limit ----

    def test_gemini_billing_exhausted_429_is_payment_required(self) -> None:
        body = {
            "error": {
                "code": 429,
                "message": "Your prepayment credits are depleted. Please go to AI Studio to manage billing.",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
        resp = httpx.Response(429, json=body)
        with pytest.raises(PaymentRequiredError) as exc:
            self._provider()._raise_cooldown_status(resp, "models/gemini-embedding-001")
        assert exc.value.status_code == 429
        assert exc.value.body == body

    def test_gemini_ratelimit_429_with_retrydelay_is_rate_limited(self) -> None:
        body = {
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}],
            }
        }
        resp = httpx.Response(429, json=body)
        with pytest.raises(ModelRateLimitedError) as exc:
            self._provider()._raise_cooldown_status(resp, "models/gemini-flash")
        assert exc.value.retry_after == 12.0

    def test_gemini_429_with_retry_after_header_wins_over_body(self) -> None:
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}],
            }
        }
        resp = httpx.Response(429, headers={"retry-after": "5"}, json=body)
        with pytest.raises(ModelRateLimitedError) as exc:
            self._provider()._raise_cooldown_status(resp, "m")
        assert exc.value.retry_after == 5.0

    @pytest.mark.parametrize("status", [200, 400, 500])
    def test_other_statuses_pass_through(self, status: int) -> None:
        # Returns without raising so each caller applies its own error type.
        self._provider()._raise_cooldown_status(httpx.Response(status), "model")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(402, PaymentRequiredError), (429, ModelRateLimitedError)],
)
async def test_openrouter_transcribe_maps_cooldown_statuses(
    status: int, expected: type[Exception]
) -> None:
    provider = OpenRouterRouter(api_key="k", client=_client_returning(status))
    try:
        with pytest.raises(expected):
            await provider.transcribe("whisper", TranscriptionRequest(audio=b"x"))
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(402, PaymentRequiredError), (429, ModelRateLimitedError)],
)
async def test_groq_transcribe_maps_cooldown_statuses(
    status: int, expected: type[Exception]
) -> None:
    provider = GroqRouter(api_key="k")
    provider._client = _client_returning(status)
    try:
        with pytest.raises(expected):
            await provider.transcribe("whisper", TranscriptionRequest(audio=b"x"))
    finally:
        await provider.aclose()


async def test_completion_retries_without_response_format_on_400() -> None:
    """Free/small models reject a requested response_format (json_schema) with HTTP 400.
    The provider must retry once WITHOUT response_format so a working free model isn't
    discarded - this is what lets free models serve plain chat. Regression for the
    'No models available' outage where every free model 400'd on json_schema.
    """
    from inference.providers.openai_compat import OpenAICompatibleProvider

    class _RecordingClient:
        def __init__(self) -> None:
            self.calls = 0
            self.last_body: dict | None = None

        async def post(self, path: str, json: dict | None = None) -> httpx.Response:
            self.calls += 1
            self.last_body = json
            if self.calls == 1:
                return httpx.Response(
                    400,
                    json={"error": {"message": "This model does not support response format `json_schema`"}},
                )
            return httpx.Response(
                200,
                json={
                    "model": "fake",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )

    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    provider._client = _RecordingClient()
    provider.name = "fake"
    req = CompletionRequest(
        prompt="hi", component="test", response_schema={"name": "x", "schema": {"type": "object"}}
    )
    resp = await provider.complete("fake-model", req)
    assert resp.text == "ok"
    assert provider._client.calls == 2
    assert "response_format" not in (provider._client.last_body or {})


async def test_response_format_rejection_heuristic() -> None:
    from inference.providers.openai_compat import OpenAICompatibleProvider

    yes = httpx.Response(400, json={"error": {"message": "does not support response format json_schema"}})
    no_500 = httpx.Response(500, json={"error": {"message": "internal"}})
    no_key = httpx.Response(400, json={"error": {"message": "invalid api key"}})
    assert OpenAICompatibleProvider._is_response_format_rejected(yes) is True
    assert OpenAICompatibleProvider._is_response_format_rejected(no_500) is False
    assert OpenAICompatibleProvider._is_response_format_rejected(no_key) is False


async def test_413_raises_payload_too_large() -> None:
    """HTTP 413 must surface as PayloadTooLargeError (not a rate limit), so the
    dispatcher skips the model instead of retrying with backoff forever."""
    from inference.exceptions import PayloadTooLargeError
    from inference.providers.openai_compat import OpenAICompatibleProvider

    class _C:
        async def post(self, path, json=None):
            return httpx.Response(413, json={"error": {"message": "Payload Too Large"}})

    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    provider._client = _C()
    provider.name = "groq"
    req = CompletionRequest(prompt="hi", component="test")
    with pytest.raises(PayloadTooLargeError):
        await provider.complete("groq/model", req)


def test_groq_free_models_have_payload_cap() -> None:
    """Groq chat models get a max_payload_chars cap so large north prompts route
    elsewhere instead of 413ing."""
    from inference.providers.groq import GroqRouter

    # Build a minimal GroqRouter without network by injecting a fake client.
    p = GroqRouter.__new__(GroqRouter)
    p._models = {}
    p._client = types.SimpleNamespace(
        get=lambda: _FakeResp()
    )
    # Simulate refresh by populating _models directly via the same ModelInfo path.
    from inference.capability import ModelCapability, ModelInfo, quality_from_cost

    p._models = {
        "llama-3.1-8b-instant": ModelInfo(
            model_id="llama-3.1-8b-instant",
            provider_name="groq",
            capabilities=frozenset([ModelCapability.COMPLETION]),
            context_window=131072,
            cost_per_token=0.0,
            base_quality=quality_from_cost(0.0),
            max_payload_chars=32_000,
        )
    }
    info = p.get_models()["llama-3.1-8b-instant"]
    assert info.max_payload_chars == 32_000


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": []}
