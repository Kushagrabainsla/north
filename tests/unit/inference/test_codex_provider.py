from __future__ import annotations

import base64
import json
import stat
import time
import urllib.parse

import httpx
import pytest

from inference.auth import ApiKeyCredentialProvider
from inference.codex_auth import CodexCredentialProvider, CodexToken, CodexTokenStore
from inference.exceptions import ProviderAuthError
from inference.factory import build_router
from inference.models import CompletionRequest, PoolPriority, ToolCallRequest
from inference.providers.openai_codex import OpenAICodexProvider, _message_items
from inference.registry import AuthKind, get_provider_definition


def _jwt(account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def test_token_store_round_trip_is_private(tmp_path) -> None:
    store = CodexTokenStore(tmp_path / "credentials" / "codex.json")
    token = CodexToken("access", "refresh", time.time() + 3600, "acct")
    store.save(token)

    assert store.load() == token
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_credentials_require_login(tmp_path) -> None:
    credentials = CodexCredentialProvider(CodexTokenStore(tmp_path / "missing.json"))
    with pytest.raises(ProviderAuthError, match="north auth login"):
        await credentials.get_auth()


@pytest.mark.asyncio
async def test_credentials_refresh_expired_token_and_preserve_rotated_token(tmp_path) -> None:
    store = CodexTokenStore(tmp_path / "codex.json")
    store.save(CodexToken("old", "old-refresh", time.time() - 1, "old-account"))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        assert b"grant_type=refresh_token" in await request.aread()
        return httpx.Response(
            200,
            json={
                "access_token": _jwt("new-account"),
                "refresh_token": "new-refresh",
                "expires_in": 7200,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    credentials = CodexCredentialProvider(store, client=client)
    auth = await credentials.get_auth()

    assert auth.headers["Authorization"].startswith("Bearer header.")
    assert auth.headers["chatgpt-account-id"] == "new-account"
    assert store.load().refresh_token == "new-refresh"  # type: ignore[union-attr]
    await client.aclose()


@pytest.mark.asyncio
async def test_browser_login_validates_callback_and_exchanges_code(tmp_path, monkeypatch) -> None:
    store = CodexTokenStore(tmp_path / "codex.json")

    async def token_handler(request: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs((await request.aread()).decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["test-code"]
        assert form["code_verifier"][0]
        return httpx.Response(200, json={
            "access_token": _jwt("browser-account"),
            "refresh_token": "browser-refresh",
            "expires_in": 3600,
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(token_handler))

    class FakeCallbackServer:
        def __init__(self, *_args) -> None:
            self.future = None
            self.loop = None

        def serve_forever(self) -> None:
            while self.future is None or self.loop is None:
                time.sleep(0.001)
            self.loop.call_soon_threadsafe(self.future.set_result, "test-code")

        def shutdown(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    seen_urls: list[str] = []
    monkeypatch.setattr("inference.codex_auth._CallbackServer", FakeCallbackServer)
    monkeypatch.setattr("inference.codex_auth.webbrowser.open", lambda url: seen_urls.append(url) or True)
    credentials = CodexCredentialProvider(store, client=client)
    status = await credentials.login()

    assert status.configured
    assert status.account_id == "browser-account"
    authorization_query = urllib.parse.parse_qs(urllib.parse.urlparse(seen_urls[0]).query)
    assert authorization_query["code_challenge_method"] == ["S256"]
    assert authorization_query["state"][0]
    assert store.load().refresh_token == "browser-refresh"  # type: ignore[union-attr]
    await client.aclose()


def test_registry_normalizes_codex_name() -> None:
    definition = get_provider_definition("openai-codex")
    assert definition.id == "openai_codex"
    assert definition.auth_kind is AuthKind.OAUTH_PKCE


def test_factory_activates_codex_from_north_owned_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))
    CodexTokenStore().save(CodexToken("access", "refresh", time.time() + 3600, "account"))

    router = build_router(openrouter_api_key="")

    assert [provider.name for provider in router._providers] == ["openai_codex"]


def test_message_conversion_preserves_tool_call_continuity() -> None:
    instructions, items = _message_items([
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Check it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "shell", "arguments": '{"cmd":"pwd"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "/tmp/project"},
    ])

    assert instructions == "Be concise"
    assert items[1] == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "shell",
        "arguments": '{"cmd":"pwd"}',
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "/tmp/project",
    }


@pytest.mark.asyncio
async def test_codex_completion_parses_sse_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        body = json.loads(await request.aread())
        assert body["store"] is False
        assert body["input"][0]["content"][0]["text"] == "hello"
        return httpx.Response(200, content=_sse(
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_text.delta", "delta": " there"},
            {
                "type": "response.completed",
                "response": {"model": "codex-model", "usage": {"input_tokens": 3, "output_tokens": 2}},
            },
        ))

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    provider = OpenAICodexProvider(ApiKeyCredentialProvider("test", "token"), client=client)
    response = await provider.complete(
        "codex-model",
        CompletionRequest(prompt="hello", component="test", priority=PoolPriority.MEDIUM),
    )

    assert response.text == "Hello there"
    assert response.tokens_in == 3
    assert response.tokens_out == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_codex_tool_stream_normalizes_function_call() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        assert body["tools"][0]["name"] == "read_file"
        return httpx.Response(200, content=_sse(
            {
                "type": "response.output_item.added",
                "item": {"id": "item-1", "type": "function_call", "call_id": "call-1", "name": "read_file"},
            },
            {"type": "response.function_call_arguments.delta", "item_id": "item-1", "delta": '{"path":'},
            {"type": "response.function_call_arguments.delta", "item_id": "item-1", "delta": '"README.md"}'},
            {
                "type": "response.completed",
                "response": {"model": "codex-model", "usage": {"input_tokens": 10, "output_tokens": 4}},
            },
        ))

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    provider = OpenAICodexProvider(ApiKeyCredentialProvider("test", "token"), client=client)
    response = await provider.complete_with_tools(
        "codex-model",
        ToolCallRequest(
            messages=[{"role": "user", "content": "read it"}],
            tools=[{"name": "read_file", "parameters": {"type": "object"}}],
            component="test",
        ),
    )

    assert response.type == "tool_calls"
    assert response.calls[0].name == "read_file"
    assert response.calls[0].call_id == "call-1"
    assert response.calls[0].params == {"path": "README.md"}
    await client.aclose()
