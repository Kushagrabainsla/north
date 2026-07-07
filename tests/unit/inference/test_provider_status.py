"""Provider HTTP-status handling: the shared cooldown policy (B8).

402 (insufficient credits) and 429/404/503 (rate limited or model gone) must
map to PaymentRequiredError / ModelRateLimitedError on both the completion and
transcription paths, so the dispatcher applies the right cooldown instead of a
generic failure.
"""

from __future__ import annotations

import httpx
import pytest

from inference.exceptions import ModelRateLimitedError, PaymentRequiredError
from inference.models import TranscriptionRequest
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

    @pytest.mark.parametrize("status", [429, 404, 503, 413])
    def test_rate_limited_statuses_raise(self, status: int) -> None:
        with pytest.raises(ModelRateLimitedError):
            self._provider()._raise_cooldown_status(httpx.Response(status), "model")

    def test_413_is_treated_as_rate_limit(self) -> None:
        # 413 (request/token-rate too large, e.g. groq TPM) must cool the model down
        # instead of falling through to a generic error that keeps hammering it.
        with pytest.raises(ModelRateLimitedError):
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
