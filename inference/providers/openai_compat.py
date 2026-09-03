"""Generic HTTP client for any provider that speaks the OpenAI wire format.

Subclasses set self.name, call super().__init__(), then optionally override
embed() or transcribe() for providers that support those capabilities.
All methods accept an explicit model_id - model selection belongs to ModelDispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from inference.auth import ApiKeyCredentialProvider, CredentialProvider
from inference.constants import DEFAULT_TIMEOUT_SECONDS, SSE_CHUNK_TIMEOUT_SECONDS
from inference.exceptions import (
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
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
        """Return the Retry-After wait in seconds (int form or HTTP-date), if present."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
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
    def _parse_duration_seconds(value: str) -> float | None:
        """Parse a protobuf Duration string (e.g. ``"12s"``, ``"0.5s"``, ``"1500ms"``)."""
        value = (value or "").strip()
        if not value:
            return None
        try:
            if value.endswith("ms"):
                return max(0.0, float(value[:-2]) / 1000.0)
            if value.endswith("s"):
                return max(0.0, float(value[:-1]))
            return max(0.0, float(value))
        except ValueError:
            return None

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
                    return OpenAICompatibleProvider._parse_duration_seconds(delay)
        return None

    @staticmethod
    def _is_billing_exhausted(status_code: int, body: dict | None, headers: dict) -> bool:
        """True when a 429/403 is permanent billing exhaustion, not a rate limit.

        Gemini (and some other Google-fronted providers) return 429 with
        ``status: RESOURCE_EXHAUSTED`` and a body like "Your prepayment credits are
        depleted" when the project's credits run out. There is no Retry-After and no
        reset window - retrying after a guessed 60s never helps. Treat these as
        PaymentRequiredError (long cooldown + surfaced as "credits needed") so
        ``north limits`` shows an honest "needs billing", not a fake countdown.
        """
        if status_code not in (429, 402, 403):
            return False
        # An explicit reset signal means it IS a transient rate limit.
        if headers.get("retry-after") or OpenAICompatibleProvider._parse_gemini_retry_delay(body) is not None:
            return False
        msg = ""
        status = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                msg = (error.get("message") or "").lower()
                status = (error.get("status") or "").upper()
        billing_markers = ("credit", "billing", "prepay", "quota", "exhausted", "insufficient")
        if status == "RESOURCE_EXHAUSTED" and not msg:
            return True
        return any(marker in msg for marker in billing_markers)

    def _raise_cooldown_status(self, response: httpx.Response, model_id: str) -> None:
        """Map HTTP status codes to typed exceptions for ModelDispatcher cooldown handling.

        401 raises ProviderAuthError (provider down).
        502/503/504 raises ProviderUnavailableError (provider down/degraded).
        402 (insufficient credits) maps to a long payment cooldown on the model.
        404 (model not found) maps to a long model cooldown without degrading the provider.
        413 (request/token-rate too large) and 429 (rate limited) map to model-level cooldowns.
        """
        if response.status_code == 401:
            raise ProviderAuthError(f"{self.name} returned 401 - provider auth failed")
        if response.status_code in (502, 503, 504):
            raise ProviderUnavailableError(
                f"{self.name} returned {response.status_code} - gateway/server outage"
            )
        if response.status_code in (402, 403):
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
            raise ProviderAuthError(f"{self.name} returned 401 - provider auth failed")
        if resp.status_code in (502, 503, 504):
            await resp.aread()
            raise ProviderUnavailableError(
                f"{self.name} returned {resp.status_code} - gateway/server outage"
            )
        if resp.status_code in (402, 403):
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
            body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
            raise InferenceError(f"{self.name} returned {resp.status_code} for {model_id}: {body}")

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
        if request.images:
            message_parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            for b64, mime in request.images:
                message_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            messages = [{"role": "user", "content": message_parts}]
        else:
            messages = [{"role": "user", "content": request.prompt}]

        body = self._build_chat_body(model_id, messages, request)

        try:
            response = await self._client.post("/chat/completions", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailableError(f"Connection to {self.name} failed: {e}") from e
        except httpx.RequestError as e:
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

        # Graceful degradation: some models (especially free/small ones) reject a
        # requested response_format (json_schema / json_object) with HTTP 400. Retry
        # once without it so a working model isn't needlessly discarded - this is what
        # lets free models serve plain chat even when they can't do structured output.
        # We retry on ANY 400 for a structured request: providers (e.g. opencode_zen)
        # wrap the real error so the body rarely names response_format explicitly.
        if response.status_code == 400 and self._should_retry_without_format(response, request):
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
            response = await self._client.post("/chat/completions", json=body)

        self._raise_for_status(response, model_id)

        try:
            payload = response.json()
        except ValueError as e:
            raise InferenceError(f"{self.name} response was not JSON") from e

        choices = payload.get("choices") or []
        if not choices:
            raise InferenceError(f"{self.name} returned empty choices for {model_id}: {payload}")
        choice = choices[0]
        native_finish = str(choice.get("native_finish_reason") or "").lower()
        finish = str(choice.get("finish_reason") or "").lower()
        if native_finish in ("network_error", "error", "failed", "upstream_error") or finish == "error":
            raise ModelDegenerateError(
                model_id,
                self.name,
                reason=f"upstream error ({native_finish or finish})",
            )
        msg_obj = choice.get("message", {})
        content = msg_obj.get("content") or ""
        reasoning = (
            msg_obj.get("reasoning")
            or msg_obj.get("reasoning_content")
            or msg_obj.get("thought")
            or None
        )
        if not content and reasoning:
            content = reasoning
        if not content:
            raise ModelDegenerateError(
                model_id,
                self.name,
                reason="empty completion text and reasoning",
            )
        usage = payload.get("usage", {})
        return CompletionResponse(
            text=content,
            model_used=payload.get("model", model_id),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost", 0.0)),
            reasoning=reasoning,
        )


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
        formatted_tools = []
        for t in request.tools:
            if "type" in t and "function" in t:
                formatted_tools.append(t)
            elif "name" in t:
                formatted_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters") or t.get("parameters_schema") or {"type": "object"},
                    },
                })
            else:
                formatted_tools.append(t)

        body: dict = {
            "model": model_id,
            "messages": request.messages,
            "stream": True,
            **self._extra_body_fields(),
            **self._request_body_fields(request),
        }
        if formatted_tools:
            body["tools"] = formatted_tools

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0
        saw_tool_call = False
        saw_reasoning = False

        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                await self._raise_for_stream_status(resp, model_id)
                async for raw_line in _aiter_with_chunk_timeout(resp.aiter_lines(), SSE_CHUNK_TIMEOUT_SECONDS):
                    if not raw_line.startswith("data: "):
                        continue
                    data = raw_line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        tokens_in = usage.get("prompt_tokens", tokens_in)
                        tokens_out = usage.get("completion_tokens", tokens_out)
                        cost_usd = float(usage.get("cost", cost_usd))
                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish = choice.get("finish_reason")
                    native_raw = choice.get("native_finish_reason") or delta.get("native_finish_reason") or ""
                    native_finish = str(native_raw).lower()
                    if native_finish in ("network_error", "error", "failed", "upstream_error") or finish == "error":
                        raise ModelDegenerateError(
                            model_id,
                            self.name,
                            reason=f"upstream stream error ({native_finish or finish})",
                        )

                    reasoning_token = (
                        delta.get("reasoning")
                        or delta.get("reasoning_content")
                        or delta.get("thought")
                        or ""
                    )
                    if reasoning_token:
                        reasoning_parts.append(reasoning_token)
                        if token_callback is not None and not saw_tool_call:
                            if not saw_reasoning:
                                saw_reasoning = True
                                await token_callback("<thought>")
                            await token_callback(reasoning_token)

                    text_token = delta.get("content") or ""
                    if text_token:
                        if saw_reasoning:
                            saw_reasoning = False
                            if token_callback is not None and not saw_tool_call:
                                await token_callback("</thought>")
                        content_parts.append(text_token)
                        # Once a tool_calls delta has arrived the response is a
                        # tool-call turn - its content never reaches the final
                        # answer, so forwarding it would show the user text
                        # that is then discarded.
                        if token_callback is not None and not saw_tool_call:
                            await token_callback(text_token)

                    for tc in (delta.get("tool_calls") or []):
                        if not saw_tool_call:
                            saw_tool_call = True
                            if saw_reasoning:
                                saw_reasoning = False
                                if token_callback is not None and hasattr(token_callback, "reset"):
                                    await token_callback("</thought>")
                            if token_callback is not None and hasattr(token_callback, "reset") and content_parts:
                                await token_callback.reset()
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_calls_acc[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls_acc[idx]["arguments"] += fn["arguments"]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            raise ProviderUnavailableError(f"Connection to {self.name} failed: {e}") from e
        except httpx.RequestError as e:
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

        if saw_reasoning and token_callback is not None and not saw_tool_call:
            await token_callback("</thought>")

        if not tool_calls_acc and not content_parts and not reasoning_parts:
            raise ModelDegenerateError(
                model_id,
                self.name,
                reason="empty stream (no content, reasoning, or tool calls)",
            )

        reasoning_text = "".join(reasoning_parts) if reasoning_parts else None

        if tool_calls_acc:
            calls = [
                ToolCall(
                    name=tc["name"],
                    call_id=tc["id"] or f"call_{tc['name']}_{idx}",
                    params=self._parse_json_args(tc["arguments"]),
                )
                for idx, tc in sorted(tool_calls_acc.items())
            ]
            return ToolCallResponse(
                type="tool_calls",
                calls=calls,
                content=None,
                model_used=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                reasoning=reasoning_text,
            )

        content_text = "".join(content_parts)
        if not content_text and reasoning_text:
            # Fallback: model generated output in reasoning channel and finished
            content_text = reasoning_text

        return ToolCallResponse(
            type="message",
            content=content_text,
            calls=[],
            model_used=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            reasoning=reasoning_text,
        )


    @staticmethod
    def _parse_json_args(raw: str) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Tool-call arguments were not valid JSON; using empty params. Raw: %.200s", raw)
            return {}

    # ---- embeddings (override in providers that support it) ----

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        raise InferenceError(f"{self.name} does not support embeddings")

    # ---- transcription (override in providers that support it) ----

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        raise TranscriptionError(f"{self.name} does not support transcription")
