from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from approval.approvals import Approvals
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import approval_policy
from tools.models import ToolInput, ToolOutput
from tools.universal.browser import BrowserTool, _CommandResult


@pytest.fixture
def settings(tmp_path):
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [
            BrowserProfile(
                id="university",
                name="University",
                purpose="University work",
                context="existing",
                data_directory=str(tmp_path / "chrome"),
                connect="9222",
            ),
            BrowserProfile(id="personal", name="Personal", purpose="Personal tasks", headed=False),
        ]
    )
    return settings


def result(**data):
    return _CommandResult(returncode=0, stdout=json.dumps({"ok": True, **data}), stderr="")


@pytest.mark.asyncio
async def test_listing_profiles_never_opens_a_browser(settings):
    tool = BrowserTool(north_settings=settings)
    with patch("tools.universal.browser._run_chrome_agent", new_callable=AsyncMock) as command:
        output = await tool.execute(ToolInput(params={"action": "list_profiles"}))
    assert output.success
    assert {p["id"] for p in output.data["profiles"]} == {"personal", "university"}
    command.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"profile_id": "missing"},
        {},
        {"profile_id": "university", "connect": "9223"},
        {"profile_id": "personal", "copy_cookies": True},
    ],
)
async def test_no_unknown_profile_fallback_or_raw_override(settings, params):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with patch("tools.universal.browser._run_chrome_agent", new_callable=AsyncMock) as command:
        output = await tool.run(ToolInput(params={"action": "inspect", **params}))
    assert not output.success
    command.assert_not_called()


@pytest.mark.asyncio
async def test_managed_preflight_reuses_the_identity_page_but_binds_each_task(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    profile = settings.browser_profiles[1]
    with (
        patch("tools.universal.browser.active_cdp_endpoint", return_value="ws://127.0.0.1:9222/devtools/browser/test"),
        patch(
            "tools.universal.browser._run_chrome_agent",
            new_callable=AsyncMock,
            side_effect=[result(), result(result=str(profile.managed_directory / "Default"))] * 2,
        ) as command,
    ):
        for task in ("one", "two"):
            output = await tool.run(
                ToolInput(params={"action": "preflight", "profile_id": "personal", "task_id": task})
            )
            assert output.success and output.data["profile_verified"]
            assert not output.data["login_verified"]
    commands = [call.args[0] for call in command.call_args_list]
    assert all(cmd[cmd.index("--browser") + 1] == "north-personal" for cmd in commands)
    assert all(cmd[cmd.index("--page") + 1] == "north-profile-check" for cmd in commands)
    assert all("chrome://version" in commands[index] for index in (0, 2))
    assert all("about:blank" not in cmd and "--purge" not in cmd and "--copy-cookies" not in cmd for cmd in commands)
    assert {key[0] for key in tool._verified_bindings} == {"one", "two"}


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["/tmp/wrong/Default", "", None])
async def test_managed_preflight_does_not_claim_success_without_correct_identity(settings, identity):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with patch(
        "tools.universal.browser._run_chrome_agent",
        new_callable=AsyncMock,
        side_effect=[result(), result(result=identity)],
    ):
        output = await tool.run(ToolInput(params={"action": "preflight", "profile_id": "personal"}))
    assert not output.success and "different profile" in output.error
    assert not tool._verified_bindings


@pytest.mark.asyncio
async def test_managed_preflight_propagates_failed_identity_navigation(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with patch(
        "tools.universal.browser._run_chrome_agent",
        new_callable=AsyncMock,
        return_value=_CommandResult(returncode=1, stdout='{"ok":false}', stderr="navigation failed"),
    ) as command:
        output = await tool.run(ToolInput(params={"action": "preflight", "profile_id": "personal"}))
    assert not output.success
    assert command.call_count == 1
    assert not tool._verified_bindings


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_existing_connection_must_match_actual_profile_before_site_action(settings, matches):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    path = str(settings.browser_profiles[0].expected_path) if matches else "/tmp/wrong/Profile 2"
    preflight = ToolOutput(success=True, data={"verified": True, "connection_id": "synthetic"})
    with (
        patch("tools.universal.browser._probe_cdp_endpoint", return_value=preflight),
        patch(
            "tools.universal.browser._run_chrome_agent",
            new_callable=AsyncMock,
            side_effect=[result(), result(result=path), result(tree="target")],
        ) as command,
    ):
        output = await tool.run(ToolInput(params={"action": "inspect", "profile_id": "university", "task_id": "task"}))
    assert output.success is matches
    assert command.call_count == (3 if matches else 2)
    assert all("--connect" in call.args[0] for call in command.call_args_list)
    if not matches:
        assert "different profile" in output.error


@pytest.mark.asyncio
async def test_describe_does_not_read_private_browser_before_initial_approval(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with patch("tools.universal.browser._run_chrome_agent", new_callable=AsyncMock) as command:
        request = await tool.describe(ToolInput(params={"action": "inspect", "profile_id": "university"}))
    assert "University" in request.message
    assert request.prepared["browser_profile"] == settings.browser_profiles[0].model_dump_json()
    command.assert_not_called()


@pytest.mark.asyncio
async def test_profile_edit_invalidates_pending_approval(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    input = ToolInput(params={"action": "preflight", "profile_id": "personal"})
    request = await tool.describe(input)
    settings.set_browser_profiles([p.model_copy(update={"purpose": "Changed work"}) for p in settings.browser_profiles])
    with patch("tools.universal.browser._run_chrome_agent", new_callable=AsyncMock) as command:
        output = await tool.run(input.model_copy(update={"approved": request}))
    assert not output.success and "changed while awaiting approval" in output.error
    command.assert_not_called()


@pytest.mark.asyncio
async def test_unbound_central_gate_refuses_existing_profile_without_connecting(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    tool.approvals = Approvals(approval_policy(), None)
    with patch("tools.universal.browser._run_chrome_agent", new_callable=AsyncMock) as command:
        output = await tool.execute(ToolInput(params={"action": "preflight", "profile_id": "university"}))
    assert not output.success and output.failure_kind == "refused"
    command.assert_not_called()


@pytest.mark.parametrize("action", ["inspect", "extract", "assert", "click", "fill", "wait", "read"])
def test_connection_and_task_tab_are_forwarded_to_every_action(settings, action):
    tool = BrowserTool(north_settings=settings)
    params = {
        "connect": "9222",
        "page": "task-one",
        "uid": "n1",
        "value": "test",
        "assert_type": "text",
        "assert_condition": "contains",
        "wait_text": "ready",
    }
    args = tool._build_args(action, params, "north-university")
    assert args[:7] == ["--json", "--browser", "north-university", "--connect", "9222", "--page", "task-one"]


@pytest.mark.asyncio
async def test_native_websocket_profile_does_not_require_http_version_endpoint(settings):
    profile = settings.browser_profiles[0].model_copy(update={"connect": "ws://localhost:9222/devtools/browser/native"})
    settings.set_browser_profiles([profile])
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with (
        patch("tools.universal.browser._probe_cdp_endpoint", side_effect=AssertionError("HTTP not supported")),
        patch(
            "tools.universal.browser._run_chrome_agent",
            new_callable=AsyncMock,
            side_effect=[result(), result(result=str(profile.expected_path))],
        ),
    ):
        output = await tool.run(ToolInput(params={"action": "preflight", "profile_id": profile.id}))
    assert output.success and output.data["profile_verified"]
    assert not output.data["login_verified"]


def test_wait_uses_vendor_condition_and_pattern():
    tool = BrowserTool()
    assert tool._build_args("wait", {"condition": "text", "pattern": "Signed in"}, "") == [
        "--json",
        "wait",
        "text",
        "Signed in",
    ]
    with pytest.raises(ValueError, match="require pattern"):
        tool._build_args("wait", {"condition": "selector"}, "")


@pytest.mark.asyncio
async def test_read_url_uses_two_supported_vendor_commands_in_same_profile(settings):
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    with (
        patch("tools.universal.browser._unsafe_url_reason", return_value=""),
        patch("tools.universal.browser.active_cdp_endpoint", side_effect=ValueError("No running browser")),
        patch(
            "tools.universal.browser._run_chrome_agent",
            new_callable=AsyncMock,
            side_effect=[result(url="https://example.com"), result(article={"title": "Example"})],
        ) as command,
    ):
        output = await tool.run(
            ToolInput(
                params={
                    "action": "read",
                    "profile_id": "personal",
                    "url": "https://example.com",
                    "task_id": "read-task",
                }
            )
        )
    assert output.success
    commands = [call.args[0] for call in command.call_args_list]
    assert "goto" in commands[0] and commands[1][-1] == "read"
    assert "read" not in commands[0]
    assert "navigation" in output.data
    assert all("north-personal" in args and "task-read-task" in args for args in commands)


@pytest.mark.asyncio
async def test_target_description_preserves_external_effect_facts_and_profile_identity(settings):
    from approval.approvals import Request
    from approval.policy import Action, ActionKind

    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    profile = settings.browser_profiles[0]
    tool._verified_bindings[("task", profile.id)] = (profile.model_dump_json(), "connection")
    request = Request(
        action=Action(
            agent="browser",
            kind=ActionKind.BROWSER,
            operation="click",
            summary="Submit one application",
            args='"Submit application"',
            reaches_third_party=True,
        ),
        title="Submit application",
        message="Sends one application",
    )
    with (
        patch.object(
            tool,
            "_verify_profile",
            new_callable=AsyncMock,
            return_value=ToolOutput(success=True, data={"connection_id": "connection"}),
        ),
        patch("tools.universal.browser.describe_browser_call", new_callable=AsyncMock, return_value=request),
    ):
        described = await tool.describe(
            ToolInput(params={"action": "click", "profile_id": profile.id, "task_id": "task", "uid": "n1"})
        )
    assert described.action.reaches_third_party
    assert "profile=university:" in described.action.args
    assert "Submit application" in described.action.args
    assert described.prepared["browser_profile"] == profile.model_dump_json()
