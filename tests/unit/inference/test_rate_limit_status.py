"""Tests for the precise, provider-aware rate-limit status store.

Covers: parsing of every provider reset signal (retry-after, retry-after-ms,
X-RateLimit-Reset in headers and in OpenRouter's error body metadata.headers,
Groq's x-ratelimit-reset-requests "1.2s" form), soonest-wait selection,
persistence round-trip, and that the dispatcher records an unavailable model
with the exact provider signal rather than a guessed constant.
"""

from __future__ import annotations

import time

import httpx
import pytest

from inference.exceptions import AllModelsRateLimitedError, ModelRateLimitedError
from inference.rate_limit_status import (
    RateLimitStatusStore,
    _parse_epoch_reset,
    _parse_groq_reset,
    compute_wait_seconds,
    format_status_markdown,
)

# ── header parsing helpers ──────────────────────────────────────────────────


def test_parse_groq_reset_seconds() -> None:
    assert _parse_groq_reset("1.2s") == pytest.approx(1.2)
    assert _parse_groq_reset("120ms") == pytest.approx(0.12)
    assert _parse_groq_reset("5") == pytest.approx(5.0)
    assert _parse_groq_reset("") is None
    assert _parse_groq_reset("soon") is None


def test_parse_epoch_reset_rejects_relative_counts() -> None:
    # A bare "0" or tiny number is not an epoch; must be ignored.
    assert _parse_epoch_reset("0") is None
    assert _parse_epoch_reset("42") is None


def test_parse_epoch_reset_accepts_epoch_ms() -> None:
    future_ms = (time.time() + 30) * 1000
    assert _parse_epoch_reset(str(future_ms)) == pytest.approx(30.0, abs=1.0)


def test_parse_epoch_reset_accepts_epoch_s() -> None:
    future_s = time.time() + 30
    assert _parse_epoch_reset(str(future_s)) == pytest.approx(30.0, abs=1.0)


# ── compute_wait_seconds: soonest positive signal wins ───────────────────────


def test_retry_after_wins_over_default() -> None:
    wait, source = compute_wait_seconds(
        status_code=429,
        headers={"retry-after": "12"},
        default=60.0,
    )
    assert wait == pytest.approx(12.0)
    assert source == "retry-after"


def test_retry_after_ms_wins_over_seconds() -> None:
    wait, source = compute_wait_seconds(
        status_code=429,
        headers={"retry-after": "30", "retry-after-ms": "5000"},
    )
    assert wait == pytest.approx(5.0)
    assert source == "retry-after-ms"


def test_openrouter_body_metadata_headers_parsed() -> None:
    # OpenRouter puts X-RateLimit-Reset in the error body, not response headers.
    body = {"metadata": {"headers": {"X-RateLimit-Reset": str(int(time.time() + 20))}}}
    wait, source = compute_wait_seconds(status_code=429, headers={}, body=body)
    assert wait == pytest.approx(20.0, abs=1.0)
    assert source.startswith("x-ratelimit-reset(body)")


def test_groq_reset_requests_duration_parsed() -> None:
    wait, source = compute_wait_seconds(
        status_code=429,
        headers={"x-ratelimit-reset-requests": "1.2s"},
    )
    assert wait == pytest.approx(1.2, abs=0.05)
    assert source == "x-ratelimit-reset-requests(dur)"


def test_no_signal_falls_back_to_default() -> None:
    wait, source = compute_wait_seconds(status_code=429, headers={}, default=60.0)
    assert wait == 60.0
    assert source == "default"


def test_bogus_huge_signal_is_ignored_for_soonest() -> None:
    # A 10_000s reset must NOT be selected when a 5s retry-after is present.
    wait, _ = compute_wait_seconds(
        status_code=429,
        headers={"retry-after": "5", "x-ratelimit-reset": str(int(time.time() + 10_000))},
    )
    assert wait == pytest.approx(5.0)


# ── store behaviour ───────────────────────────────────────────────────────────


def test_record_rate_limit_persists_and_reloads(tmp_path) -> None:
    path = tmp_path / "rls.json"
    store = RateLimitStatusStore(path)
    store.record_rate_limit(
        "gemini", "gemini-flash", status_code=429, headers={"retry-after": "12"}, is_free=True
    )
    assert store.is_active("gemini", "gemini-flash")
    rec = store.snapshot()[0]
    assert rec.wait_seconds == pytest.approx(12.0)
    assert rec.source == "retry-after"
    assert rec.is_free is True

    # Reload from disk - active record survives.
    reloaded = RateLimitStatusStore(path)
    reloaded.load()
    assert reloaded.is_active("gemini", "gemini-flash")
    assert reloaded.snapshot()[0].wait_seconds == pytest.approx(12.0)


def test_payment_required_records_24h(tmp_path) -> None:
    store = RateLimitStatusStore(tmp_path / "rls.json")
    rec = store.record_payment_required("openrouter", "anthropic/claude", is_free=False)
    assert rec.kind == "payment_required"
    assert rec.wait_seconds == 86_400.0
    assert rec.is_free is False


def test_compute_wait_seconds_handles_list_body() -> None:
    """A provider may return a JSON *list* as the error body (some OpenAI-compatible
    proxies do). compute_wait_seconds must not crash on body.get(...) - it should
    just fall back to the default rather than raising AttributeError mid-dispatch.
    """
    wait, source = compute_wait_seconds(status_code=429, headers={}, body=[{"error": "x"}])
    assert wait == 60.0
    assert source == "default"
    # And a totally non-dict body is fine too.
    wait2, _ = compute_wait_seconds(status_code=429, headers={}, body="rate limited")
    assert wait2 == 60.0


def test_compute_wait_seconds_picks_up_gemini_retryinfo() -> None:
    """Gemini's OpenAI-compat 429 carries the precise reset in the body's
    error.details RetryInfo.retryDelay (Duration string) - that must win over
    the guessed default so north retries exactly when Google says.
    """
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}],
        }
    }
    wait, source = compute_wait_seconds(status_code=429, headers={}, body=body)
    assert wait == 12.0
    assert source == "retryinfo(dur)"


def test_provider_down_is_provider_wide(tmp_path) -> None:
    store = RateLimitStatusStore(tmp_path / "rls.json")
    store.record_provider_down("groq", "provider auth failed")
    # Model-scoped query must also see the provider-wide block.
    assert store.is_active("groq", "any-model")


def test_record_error_captures_transient_failure(tmp_path) -> None:
    store = RateLimitStatusStore(tmp_path / "rls.json")
    rec = store.record_error("gemini", "gemini-flash", reason="JSON parse failed")
    assert rec.kind == "error"
    assert rec.source == "error"
    assert rec.wait_seconds == 60.0  # short window, auto-clears if it recovers
    assert store.is_active("gemini", "gemini-flash")
    md = format_status_markdown(tmp_path / "rls.json")
    assert "ERR" in md
    assert "JSON parse failed" in md


def test_mark_ok_tracks_checked_count(tmp_path) -> None:
    store = RateLimitStatusStore(tmp_path / "rls.json")
    assert store.checked_count() == 0
    store.mark_ok("openrouter", "anthropic/claude")
    store.mark_ok("gemini", "gemini-flash")
    assert store.checked_count() == 2
    # mark_ok does NOT create an unavailable record
    assert not store.is_active("openrouter", "anthropic/claude")


def test_format_status_unknown_footer(tmp_path) -> None:
    # No failures but pool only partially probed -> honest "unknown" note.
    text = format_status_markdown(
        tmp_path / "missing.json", checked=3, pool_total=10
    )
    assert "no active cooldowns" in text
    assert "3/10" in text
    assert "not yet tried" in text


def test_expired_records_dropped_on_load(tmp_path) -> None:
    path = tmp_path / "rls.json"
    store = RateLimitStatusStore(path)
    store.record_rate_limit("openrouter", "m", status_code=429, headers={"retry-after": "0.01"})
    time.sleep(0.05)
    reloaded = RateLimitStatusStore(path)
    reloaded.load()
    assert not reloaded.is_active("openrouter", "m")
    assert reloaded.snapshot() == []


def test_format_status_markdown_renders_active_and_empty(tmp_path) -> None:
    from inference.rate_limit_status import format_status_markdown

    # No file -> "no failures recorded"
    assert "no active cooldowns recorded" in format_status_markdown(tmp_path / "missing.json")

    path = tmp_path / "rls.json"
    store = RateLimitStatusStore(path)
    store.record_rate_limit("gemini", "gemini-flash", status_code=429, headers={"retry-after": "12"}, is_free=True)
    store.record_payment_required("openrouter", "anthropic/claude", is_free=False)
    md = format_status_markdown(path)
    # Shared by both `north limits` and the Telegram /limits command.
    assert "rate-limit status" in md
    assert "gemini-flash" in md
    assert "free" in md
    assert "back at" in md
    assert "anthropic/claude" in md  # payment record also shown
    assert "retry-after" in md  # precise signal surfaced


# ── dispatcher records the precise signal (integration) ──────────────────────


def _resp(status: int, headers: dict[str, str], body: dict) -> httpx.Response:
    return httpx.Response(status, headers=headers, json=body)


def test_provider_attaches_status_and_headers_to_exception() -> None:
    resp = _resp(429, {"retry-after": "9"}, {"error": {"message": "rate limit"}})
    with pytest.raises(ModelRateLimitedError) as exc:
        from inference.providers.openai_compat import OpenAICompatibleProvider

        OpenAICompatibleProvider(name="t", base_url="http://t", api_key="k")._raise_cooldown_status(resp, "model-x")
    assert exc.value.retry_after == 9.0
    assert exc.value.status_code == 429
    assert exc.value.headers.get("retry-after") == "9"
    assert exc.value.body == {"error": {"message": "rate limit"}}


def test_dispatcher_records_status_with_precise_wait(tmp_path) -> None:
    """A 429 in the dispatch chain is recorded with the provider's real wait."""
    from config.strategy import NorthSettings
    from inference.capability import ModelCapability, ModelInfo
    from inference.dispatcher import ModelDispatcher
    from inference.provider import Provider

    class _Bad(Provider):
        name = "bad"

        def get_models(self):
            return {
                "m": ModelInfo(
                    model_id="m",
                    provider_name="bad",
                    capabilities=frozenset({ModelCapability.COMPLETION}),
                    context_window=100_000,
                    cost_per_token=0.0,
                    base_quality=0.5,
                )
            }

        async def complete(self, model_id, request):
            raise ModelRateLimitedError(
                model_id,
                "bad",
                retry_after=7.0,
                status_code=429,
                headers={"retry-after": "7"},
                body={"error": {"message": "rate limit"}},
            )

        async def complete_with_tools(self, model_id, request, token_callback=None):
            raise NotImplementedError

        async def embed(self, model_id, request):
            raise NotImplementedError

        async def transcribe(self, model_id, request):
            raise NotImplementedError

        async def refresh(self):
            return None

        async def aclose(self):
            return None

    disp = ModelDispatcher(
        providers=[_Bad()],
        north_settings=NorthSettings(tmp_path / "settings.json", default_preferred_models={}),
        cooldowns_path=tmp_path / "cd.json",
    )
    from inference.models import CompletionRequest

    with pytest.raises(AllModelsRateLimitedError):
        # No healthy candidate -> AllModelsRateLimitedError after recording.
        import asyncio

        asyncio.run(disp.complete(CompletionRequest(prompt="hi", component="test")))

    status = disp.rate_limit_status()
    assert len(status) == 1
    rec = status[0]
    assert rec["provider"] == "bad"
    assert rec["model"] == "m"
    assert rec["wait_seconds"] == pytest.approx(7.0)
    assert rec["source"] == "retry-after"
    assert rec["is_free"] is True
