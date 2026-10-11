"""The private SDK bridge does not own approvals or retry uncertain actions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from config.browser_profiles import BrowserProfile
from mcp.client import McpError
from mcp.models import McpCallResult
from tools.universal.browser_native import ExistingChrome


@pytest.fixture
def profile(tmp_path):
    return BrowserProfile(
        id="university",
        name="University",
        purpose="University tasks",
        context="existing",
        data_directory=str(tmp_path / "chrome"),
        profile_directory="Profile 3",
    )


def response(**data):
    return McpCallResult(content=[{"type": "text", "text": json.dumps(data)}])


@pytest.mark.asyncio
async def test_native_websocket_never_needs_http_discovery(profile, tmp_path):
    client = AsyncMock()
    client.call_tool.return_value = response(profile_verified=True, login_verified=False, window_id="one")
    adapter = ExistingChrome(tmp_path)
    adapter._client = client
    endpoint = "ws://127.0.0.1:9222/devtools/browser/native"
    with patch("tools.universal.browser_native.httpx.AsyncClient", side_effect=AssertionError("No HTTP")):
        output = await adapter.call(profile, {"action": "preflight", "task_id": "task", "connect": endpoint})
    assert output.success and output.data["profile_verified"]
    assert not output.data["login_verified"]
    arguments = client.call_tool.call_args.args[1]
    assert arguments["endpoint"] == endpoint
    assert arguments["task_id"] == "task" and arguments["profile_id"] == profile.id
    assert Path(arguments["expected_path"]) == profile.expected_path


@pytest.mark.asyncio
async def test_socket_success_without_identity_is_not_profile_success(profile, tmp_path):
    client = AsyncMock()
    client.call_tool.return_value = response(verified=True)
    adapter = ExistingChrome(tmp_path)
    adapter._client = client
    output = await adapter.call(
        profile,
        {
            "action": "preflight",
            "task_id": "task",
            "connect": "ws://localhost:9222/devtools/browser/native",
        },
    )
    assert not output.success and "identity" in output.error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [McpError("Timeout"), TimeoutError()])
async def test_transport_timeout_closes_worker_and_never_replays_mutation(profile, tmp_path, error):
    client = AsyncMock()
    client.call_tool.side_effect = error
    adapter = ExistingChrome(tmp_path)
    adapter._client = client
    output = await adapter.call(
        profile,
        {
            "action": "click",
            "task_id": "task",
            "uid": "n1",
            "connect": "ws://localhost:9222/devtools/browser/native",
        },
    )
    assert not output.success
    client.call_tool.assert_awaited_once()
    client.close.assert_awaited_once()
    assert adapter._client is None


@pytest.mark.asyncio
async def test_missing_task_id_never_starts_a_worker(profile, tmp_path):
    adapter = ExistingChrome(tmp_path)
    with patch.object(adapter, "_start", new_callable=AsyncMock) as start:
        output = await adapter.call(profile, {"action": "preflight", "connect": "9222"})
    assert not output.success and "task_id" in output.error
    start.assert_not_called()


@pytest.mark.asyncio
async def test_remote_endpoint_is_rejected_before_worker_start(profile, tmp_path):
    adapter = ExistingChrome(tmp_path)
    with patch.object(adapter, "_start", new_callable=AsyncMock) as start:
        output = await adapter.call(
            profile, {"action": "preflight", "task_id": "task", "connect": "ws://remote.example/devtools/browser/a"}
        )
    assert not output.success
    start.assert_not_called()


@pytest.mark.asyncio
async def test_description_reads_use_internal_purpose_not_model_flags(profile, tmp_path):
    client = AsyncMock()
    client.call_tool.return_value = response(profile_verified=True, result='{"url":"https://example.com"}')
    adapter = ExistingChrome(tmp_path)
    adapter._client = client
    params = {
        "action": "click",
        "task_id": "task",
        "purpose": "untrusted",
        "connect": "ws://localhost:9222/devtools/browser/a",
    }
    runner = adapter.description_runner(profile, params)
    await runner(["eval", "fixed description script"], 5)
    assert client.call_tool.call_args.args[1]["purpose"] == "description"
    client.call_tool.reset_mock()
    await adapter.call(profile, {**params, "action": "eval", "js": "user script"})
    assert "purpose" not in client.call_tool.call_args.args[1]


@pytest.mark.asyncio
async def test_failed_assertion_returns_failed_tool_output(profile, tmp_path):
    client = AsyncMock()
    client.call_tool.return_value = response(profile_verified=True, held=False)
    adapter = ExistingChrome(tmp_path)
    adapter._client = client
    output = await adapter.call(
        profile,
        {
            "action": "assert",
            "task_id": "task",
            "connect": "ws://localhost:9222/devtools/browser/a",
        },
    )
    assert not output.success and output.data["held"] is False


@pytest.mark.asyncio
async def test_disconnect_only_closes_private_transport(tmp_path):
    adapter = ExistingChrome(tmp_path)
    client = AsyncMock()
    adapter._client = client
    await adapter.aclose()
    client.close.assert_awaited_once()
    client.call_tool.assert_not_called()
