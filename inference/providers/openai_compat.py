"""Generic HTTP client for any provider that speaks the OpenAI wire format.

Subclasses set self.name, call super().__init__(), then optionally override
embed() or transcribe() for providers that support those capabilities.
All methods accept an explicit model_id - model selection belongs to ModelDispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from inference.auth import ApiKeyCredentialProvider, CredentialProvider
from inference.constants import DEFAULT_TIMEOUT_SECONDS, SSE_CHUNK_TIMEOUT_SECONDS
from inference.durations import parse_duration_seconds
from inference.exceptions import (
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    ModelRefusedError,
    PayloadTooLargeError,
    PaymentRequiredError,
    ProviderAuthError,
    ProviderUnavailableError,
    TranscriptionError,
)
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.usage import cache_tokens

logger = logging.getLogger(__name__)


async def _aiter_with_chunk_timeout(aiter, timeout: float):
    """Wrap an async iterator, raising InferenceError if a chunk takes too long."""
    while True:
        try:
            yield await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise InferenceError(f"SSE stream stalled for {timeout:.0f}s - model stopped generating") from exc


async def _aiter_sse_chunks(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield each JSON chunk of an OpenAI-style SSE stream, skipping unparsable lines."""
    async for raw_line in _aiter_with_chunk_timeout(response.aiter_lines(), SSE_CHUNK_TIMEOUT_SECONDS):
        if not raw_line.startswith("data: "):
            continue
        data = raw_line[6:]
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        yield chunk


# Finish reasons that mean the upstream provider broke mid-stream rather than
# the model finishing: the reply is unusable and the model must be failed over.
_UPSTREAM_ERROR_REASONS = frozenset({"network_error", "error", "failed", "upstream_error"})


def _parse_json_args(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Tool-call arguments were not valid JSON; using empty params. Raw: %.200s", raw)
        return {}


def _formatted_tool(tool: dict) -> dict:
    """One tool in the OpenAI function-calling shape, whatever shape it arrived in."""
    if "type" in tool and "function" in tool:
        return tool
    if "name" not in tool:
        return tool
    return {
        "type": "function",
        "function": {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or tool.get("parameters_schema") or {"type": "object"},
        },
    }


def _chat_messages(request: CompletionRequest) -> list[dict]:
    """The user turn, with any images attached as data URLs."""
    if not request.images:
        return [{"role": "user", "content": request.prompt}]
    parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
    parts.extend(
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}} for b64, mime in request.images
    )
    return [{"role": "user", "content": parts}]


@dataclass
class _StreamUsage:
    """What the stream reported about its own cost, as the last usage block saw it."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cached_tokens: int = 0
    cache_write_tokens: int = 0

    def update(self, usage: dict) -> None:
        self.tokens_in = usage.get("prompt_tokens", self.tokens_in)
        self.tokens_out = usage.get("completion_tokens", self.tokens_out)
        self.cost_usd = float(usage.get("cost", self.cost_usd))
        self.cached_tokens, self.cache_write_tokens = cache_tokens(usage)


class _ToolCallStream:
    """Folds one streamed tool-call turn into text, reasoning and tool calls.

    A turn is either an answer or a tool call, and which one it is only becomes
    known when the first `tool_calls` delta arrives. Everything streamed to the
    caller before that point belongs to an answer that is now being discarded,
    which is why the switch retracts it.
    """

    def __init__(
        self,
        model_id: str,
        provider: str,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._model_id = model_id
        self._provider = provider
        self._emit = token_callback
        self._usage = _StreamUsage()
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, dict] = {}
        self._saw_tool_call = False
        self._in_thought = False

    @property
    def _forwarding(self) -> bool:
        """True while streamed tokens still belong to the answer the caller sees."""
        return self._emit is not None and not self._saw_tool_call

    async def _forward(self, token: str) -> None:
        """Send one token on, if there is anywhere to send it.

        Every call site is already guarded by ``_forwarding``, which implies a
        callback exists - but it says so through a property, and no checker can
        follow that back to the attribute. Narrowing here once beats asserting at
        six call sites.
        """
        if self._emit is not None:
            await self._emit(token)

    async def add(self, chunk: dict) -> None:
        usage = chunk.get("usage")
        if usage:
            self._usage.update(usage)
        choices = chunk.get("choices")
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta", {})
        self._raise_if_upstream_failed(choice, delta)
        await self._add_reasoning(delta)
        await self._add_text(delta)
        await self._add_tool_calls(delta)

    async def close(self) -> None:
        """End the turn, raising when the model streamed nothing at all."""
        if self._in_thought and self._forwarding:
            await self._forward("</thought>")
        if not self._calls and not self._content and not self._reasoning:
            raise ModelDegenerateError(
                self._model_id,
                self._provider,
                reason="empty stream (no content, reasoning, or tool calls)",
            )

    def to_response(self) -> ToolCallResponse:
        reasoning_text = "".join(self._reasoning) or None
        if self._calls:
            return self._response(calls=self._tool_calls(), content=None, reasoning=reasoning_text)
        # Fallback: model generated output in reasoning channel and finished.
        content_text = "".join(self._content) or reasoning_text or ""
        return self._response(calls=[], content=content_text, reasoning=reasoning_text)

    def _raise_if_upstream_failed(self, choice: dict, delta: dict) -> None:
        finish = choice.get("finish_reason")
        native = str(choice.get("native_finish_reason") or delta.get("native_finish_reason") or "").lower()
        if native in _UPSTREAM_ERROR_REASONS or finish == "error":
            raise ModelDegenerateError(
                self._model_id,
                self._provider,
                reason=f"upstream stream error ({native or finish})",
            )

    async def _add_reasoning(self, delta: dict) -> None:
        token = delta.get("reasoning") or delta.get("reasoning_content") or delta.get("thought") or ""
        if not token:
            return
        self._reasoning.append(token)
        if not self._forwarding:
            return
        if not self._in_thought:
            self._in_thought = True
            await self._forward("<thought>")
        await self._forward(token)

    async def _add_text(self, delta: dict) -> None:
        token = delta.get("content") or ""
        if not token:
            return
        if self._in_thought:
            self._in_thought = False
            if self._forwarding:
                await self._forward("</thought>")
        self._content.append(token)
        # Once a tool_calls delta has arrived the response is a tool-call turn -
        # its content never reaches the final answer, so forwarding it would show
        # the user text that is then discarded.
        if self._forwarding:
            await self._forward(token)

    async def _add_tool_calls(self, delta: dict) -> None:
        for call in delta.get("tool_calls") or []:
            if not self._saw_tool_call:
                self._saw_tool_call = True
                await self._retract_streamed_answer()
            self._accumulate_call(call)

    async def _retract_streamed_answer(self) -> None:
        """Take back the answer streamed so far: this turn is a tool call."""
        # Held as the bound method rather than as a flag: the callback may or may
        # not offer `reset`, and asking once keeps the two uses in agreement.
        reset = getattr(self._emit, "reset", None)
        if self._in_thought:
            self._in_thought = False
            if reset is not None:
                await self._forward("</thought>")
        if reset is not None and self._content:
            await reset()

    def _accumulate_call(self, call: dict) -> None:
        entry = self._calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
        if call.get("id"):
            entry["id"] = call["id"]
        function = call.get("function", {})
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]

    def _tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(
                name=call["name"],
                call_id=call["id"] or f"call_{call['name']}_{index}",
                params=_parse_json_args(call["arguments"]),
            )
            for index, call in sorted(self._calls.items())
        ]

    def _response(self, *, calls: list[ToolCall], content: str | None, reasoning: str | None) -> ToolCallResponse:
        return ToolCallResponse(
            type="tool_calls" if calls else "message",
            calls=calls,
            content=content,
            model_used=self._model_id,
            tokens_in=self._usage.tokens_in,
            tokens_out=self._usage.tokens_out,
            cost_usd=self._usage.cost_usd,
            cached_tokens=self._usage.cached_tokens,
            cache_write_tokens=self._usage.cache_write_tokens,
            reasoning=reasoning,
        )


class OpenAICompatibleProvider:
    """Base class for providers that use the OpenAI wire format over HTTPS.

    Handles all HTTP mechanics. Subclasses supply provider-specific details:
    name, base_url, api_key, and optional overrides for embed/transcribe.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        *,
        credentials: CredentialProvider | None = None,
    ) -> None:
        self.name = name
        self._credentials = credentials or ApiKeyCredentialProvider(name, api_key)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout=DEFAULT_TIMEOUT_SECONDS, connect=5.0),
            event_hooks={"request": [self._authenticate_request]},
        )

    async def _authenticate_request(self, request: httpx.Request) -> None:
        """Resolve credentials for every request so OAuth refresh is transparent."""
        auth = await self._credentials.get_auth()
        request.headers.update(auth.headers)

    # ---- status helpers ----

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Return the wait in seconds the provider reported, from whichever header carries it.

        ``Retry-After`` is the standard one. ``X-RateLimit-Reset`` is what
        OpenRouter sends instead, and reading it is what tells a per-minute pace
        limit (seconds away) apart from a daily allowance (hours away) - the
        distinction :mod:`inference.failure` needs to scope the failure without
        having to recognise anyone's wording.
        """
        raw = response.headers.get("retry-after")
        if not raw:
            return OpenAICompatibleProvider._parse_reset_header(response.headers)
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            from datetime import UTC, datetime
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(raw)
            return max(0.0, (when - datetime.now(when.tzinfo or UTC)).total_seconds())
        except Exception:
            return None

    @staticmethod
    def _parse_reset_header(headers: httpx.Headers) -> float | None:
        """Seconds until ``X-RateLimit-Reset``, which providers stamp in epoch ms or s.

        The two are told apart by magnitude rather than by configuration: an
        epoch in seconds is a ten-digit number for the next few centuries, so
        anything far above that is milliseconds.
        """
        raw = (headers.get("x-ratelimit-reset") or "").strip()
        if not raw:
            return None
        try:
            reset = float(raw)
        except ValueError:
            return None
        if reset > 1e11:  # milliseconds since the epoch
            reset /= 1000.0
        return max(0.0, reset - time.time())

    @staticmethod
    def _parse_gemini_retry_delay(body: dict | None) -> float | None:
        """Extract Google's precise retry signal from a 429 error body.

        Gemini's OpenAI-compatible endpoint returns a standard Google RPC error:
        error.details may carry a RetryInfo with ``retryDelay`` (a Duration string
        like "12s" or "0.5s"). When present this is the authoritative "try again
        at" time and should win over a guessed cooldown.
        """
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        details = error.get("details")
        if not isinstance(details, list):
            return None
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if detail.get("@type", "").endswith("RetryInfo"):
                delay = detail.get("retryDelay")
                if isinstance(delay, str):
                    return parse_duration_seconds(delay)
        return None

    @staticmethod
    def _is_billing_exhausted(status_code: int, body: dict | None, headers: dict) -> bool:
        """Whether this reply means "fund the account", when the status code does not say so.

        The default is *no*, because the status codes already say it: 402 is
        money, 429 is pace, 401 is the key. A provider that honours them needs no
        help reading its replies, and guessing at their wording actively hurt -
        scanning every reply for the word "credit" turned OpenRouter's free-tier
        upsell ("Add 10 credits to unlock 1000 free model requests per day") into
        a billing verdict against fourteen working free models.

        A provider that genuinely misreports overrides this. :class:`GeminiRouter`
        does, because Gemini reports depleted prepaid credit as a 429; OpenCode
        Zen handles its own 401 quirk in :meth:`_raise_cooldown_status`. Each
        exception then lives beside the provider it describes, and cannot reach
        the providers it does not.
        """
        return False

    def _raise_cooldown_status(self, response: httpx.Response, model_id: str) -> None:
        """Map HTTP status codes to typed exceptions for ModelDispatcher cooldown handling.

        401 raises ProviderAuthError (provider down).
        502/503/504 raises ProviderUnavailableError (provider down/degraded).
        402 (insufficient credits) maps to a long payment cooldown on the model.
        403 is a refusal of *this request* - a content or policy rule, not a bill -
        so it falls through to a model-scoped InferenceError rather than claiming
        the account needs funding. A provider that bills through 403 says so by
        overriding :meth:`_is_billing_exhausted`.
        404 (model not found) maps to a long model cooldown without degrading the provider.
        413 (request/token-rate too large) and 429 (rate limited) map to model-level cooldowns.
        """
        if response.status_code == 401 and not self._is_billing_exhausted(
            401, self._safe_json(response), dict(response.headers)
        ):
            raise ProviderAuthError(f"{self.name} returned 401 - provider auth failed")
        if response.status_code in (502, 503, 504):
            raise ProviderUnavailableError(f"{self.name} returned {response.status_code} - gateway/server outage")
        if response.status_code == 403 and not self._is_billing_exhausted(
            403, self._safe_json(response), dict(response.headers)
        ):
            raise ModelRefusedError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=self._safe_json(response),
            )
        if response.status_code in (401, 402, 403):
            raise PaymentRequiredError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=self._safe_json(response),
            )
        if response.status_code == 404:
            raise ModelNotFoundError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=self._safe_json(response),
            )
        if response.status_code == 413:
            # Request/payload too large. Permanent for this prompt - no Retry-After
            # applies, so surface as PayloadTooLargeError (not a rate limit) so the
            # dispatcher skips this model instead of hammering it with backoff.
            raise PayloadTooLargeError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=self._safe_json(response),
            )
        if response.status_code == 429:
            headers = dict(response.headers)
            body = self._safe_json(response)
            # Gemini (and other Google-fronted providers) return 429 with
            # RESOURCE_EXHAUSTED + "credits depleted" when billing is empty - that is
            # permanent, not a rate limit, so surface it as PaymentRequiredError
            if self._is_billing_exhausted(response.status_code, body, headers):
                raise PaymentRequiredError(
                    model_id,
                    self.name,
                    status_code=response.status_code,
                    headers=headers,
                    body=body,
                )
            retry_after = self._parse_retry_after(response) or self._parse_gemini_retry_delay(body)
            raise ModelRateLimitedError(
                model_id,
                self.name,
                retry_after=retry_after,
                status_code=response.status_code,
                headers=headers,
                body=body,
            )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict | None:
        """Best-effort JSON parse of the response body; returns None if not JSON."""
        try:
            return response.json()
        except Exception:
            return None

    def _raise_for_status(self, response: httpx.Response, model_id: str) -> None:
        self._raise_cooldown_status(response, model_id)
        if response.status_code >= 400:
            raise InferenceError(f"{self.name} returned {response.status_code} for {model_id}: {response.text[:200]}")

    async def _raise_for_stream_status(self, resp: httpx.Response, model_id: str) -> None:
        if resp.status_code == 401:
            await resp.aread()
            if not self._is_billing_exhausted(401, self._safe_json(resp), dict(resp.headers)):
                raise ProviderAuthError(f"{self.name} returned 401 - provider auth failed")
        if resp.status_code in (502, 503, 504):
            await resp.aread()
            raise ProviderUnavailableError(f"{self.name} returned {resp.status_code} - gateway/server outage")
        if resp.status_code == 403:
            await resp.aread()
            if not self._is_billing_exhausted(403, self._safe_json(resp), dict(resp.headers)):
                raise ModelRefusedError(
                    model_id,
                    self.name,
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                    body=self._safe_json(resp),
                )
        if resp.status_code in (401, 402, 403):
            await resp.aread()
            raise PaymentRequiredError(
                model_id,
                self.name,
                status_code=resp.status_code,
                headers=dict(resp.headers),
                body=self._safe_json(resp),
            )
        if resp.status_code == 404:
            await resp.aread()
            raise ModelNotFoundError(
                model_id,
                self.name,
                status_code=resp.status_code,
                headers=dict(resp.headers),
                body=self._safe_json(resp),
            )
        if resp.status_code == 413:
            await resp.aread()
            headers = dict(resp.headers)
            body = self._safe_json(resp)
            raise PayloadTooLargeError(
                model_id,
                self.name,
                status_code=resp.status_code,
                headers=headers,
                body=body,
            )

        if resp.status_code == 429:
            await resp.aread()
            headers = dict(resp.headers)
            body = self._safe_json(resp)
            if self._is_billing_exhausted(resp.status_code, body, headers):
                raise PaymentRequiredError(
                    model_id,
                    self.name,
                    status_code=resp.status_code,
                    headers=headers,
                    body=body,
                )
            retry_after = self._parse_retry_after(resp) or self._parse_gemini_retry_delay(body)
            raise ModelRateLimitedError(
                model_id,
                self.name,
                retry_after=retry_after,
                status_code=resp.status_code,
                headers=headers,
                body=body,
            )
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode("utf-8", errors="replace")[:200]
            raise InferenceError(f"{self.name} returned {resp.status_code} for {model_id}: {detail}")

    async def aclose(self) -> None:
        """Close the underlying HTTPX client."""
        await self._client.aclose()

    def _extra_body_fields(self) -> dict:
        """Provider-specific fields to merge into every request body.

        Override in subclasses that require non-standard fields.
        Example: OpenRouterProvider adds {"usage": {"include": True}}.
        """
        return {}

    def _request_body_fields(self, request: CompletionRequest | ToolCallRequest) -> dict:
        """Provider-specific fields derived from one inference request."""
        return {}

    # ---- completion ----

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        body = self._build_chat_body(model_id, _chat_messages(request), request)
        response = await self._post_chat(body)

        # Graceful degradation: some models (especially free/small ones) reject a
        # requested response_format (json_schema / json_object) with HTTP 400. Retry
        # once without it so a working model isn't needlessly discarded - this is what
        # lets free models serve plain chat even when they can't do structured output.
        # We retry on ANY 400 for a structured request: providers (e.g. opencode_zen)
        # wrap the real error so the body rarely names response_format explicitly.
        if response.status_code == 400 and self._should_retry_without_format(response, request):
            response = await self._retry_without_response_format(body, model_id)

        self._raise_for_status(response, model_id)
        payload = self._decoded(response)
        choice = self._first_choice(payload, model_id)
        self._raise_if_upstream_failed(choice, model_id)
        content, reasoning = self._answer_of(choice, model_id)

        usage = payload.get("usage", {})
        cached, cache_written = cache_tokens(usage)
        return CompletionResponse(
            text=content,
            model_used=payload.get("model", model_id),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost", 0.0)),
            cached_tokens=cached,
            cache_write_tokens=cache_written,
            reasoning=reasoning,
        )

    async def _post_chat(self, body: dict) -> httpx.Response:
        try:
            return await self._client.post("/chat/completions", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailableError(f"Connection to {self.name} failed: {e}") from e
        except httpx.RequestError as e:
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

    async def _retry_without_response_format(self, body: dict, model_id: str) -> httpx.Response:
        # Say so. The retry turns a schema-enforced call into a free-form one,
        # and a caller that gets prose back where it asked for JSON has no
        # other way to find out this is why. The dispatcher's validity gate
        # rejects the prose and moves on, but the reason belongs in the log.
        logger.warning(
            "%s/%s rejected the requested response_format - retrying without it, "
            "so this response is NOT schema-enforced",
            self.name,
            model_id,
        )
        body.pop("response_format", None)
        return await self._post_chat(body)

    def _decoded(self, response: httpx.Response) -> dict:
        try:
            return response.json()
        except ValueError as e:
            raise InferenceError(f"{self.name} response was not JSON") from e

    def _first_choice(self, payload: dict, model_id: str) -> dict:
        choices = payload.get("choices") or []
        if not choices:
            raise InferenceError(f"{self.name} returned empty choices for {model_id}: {payload}")
        return choices[0]

    def _raise_if_upstream_failed(self, choice: dict, model_id: str) -> None:
        native = str(choice.get("native_finish_reason") or "").lower()
        finish = str(choice.get("finish_reason") or "").lower()
        if native in _UPSTREAM_ERROR_REASONS or finish == "error":
            raise ModelDegenerateError(model_id, self.name, reason=f"upstream error ({native or finish})")

    def _answer_of(self, choice: dict, model_id: str) -> tuple[str, str | None]:
        """The text and reasoning of a finished completion, one of which must be there."""
        message = choice.get("message", {})
        reasoning = message.get("reasoning") or message.get("reasoning_content") or message.get("thought") or None
        content = message.get("content") or reasoning or ""
        if not content:
            raise ModelDegenerateError(model_id, self.name, reason="empty completion text and reasoning")
        return content, reasoning

    def _build_chat_body(self, model_id: str, messages: list[dict], request: CompletionRequest) -> dict:
        body: dict = {
            "model": model_id,
            "messages": messages,
            **self._extra_body_fields(),
            **self._request_body_fields(request),
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        schema = request.structured_schema
        if schema is not None:
            # Structured output with JSON Schema (takes precedence over json_mode)
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        elif request.json_mode:
            # Legacy JSON object mode
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _should_retry_without_format(response: httpx.Response, request: CompletionRequest) -> bool:
        """True if a 400 on this request should be retried without response_format.

        We retry on ANY 400 for a structured request (json_mode / response_schema):
        providers (e.g. opencode_zen) wrap the underlying rejection so the body rarely
        names response_format explicitly, and a working free model is discarded if we
        wait for an exact keyword match. Non-structured 400s are real errors (bad key,
        etc.) and must NOT be retried this way.
        """
        if response.status_code != 400:
            return False
        return request.response_schema is not None or request.json_mode

    # ---- tool calls ----

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        body: dict = {
            "model": model_id,
            "messages": request.messages,
            "stream": True,
            **self._extra_body_fields(),
            **self._request_body_fields(request),
        }
        if request.tools:
            body["tools"] = [_formatted_tool(tool) for tool in request.tools]

        stream = _ToolCallStream(model_id, self.name, token_callback)
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                await self._raise_for_stream_status(resp, model_id)
                async for chunk in _aiter_sse_chunks(resp):
                    await stream.add(chunk)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailableError(f"Connection to {self.name} failed: {e}") from e
        except httpx.RequestError as e:
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

        await stream.close()
        return stream.to_response()

    # ---- embeddings (override in providers that support it) ----

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        raise InferenceError(f"{self.name} does not support embeddings")

    # ---- transcription (override in providers that support it) ----

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        raise TranscriptionError(f"{self.name} does not support transcription")
