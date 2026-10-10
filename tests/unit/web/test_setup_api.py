from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.browser_profiles import BrowserProfile, SetupProgress
from config.strategy import NorthSettings
from orchestrator.api.settings import SettingsUpdate, update_settings
from orchestrator.api_context import ApiServices, attach, bind_services
from tests.conftest import approval_policy
from tools.registry import ToolRegistry
from tools.universal.browser import BrowserTool, _CommandResult
from utils.tasks import drain
from web import extensions as setup
from web.api import WebRuntime, router, session_router


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "PROVIDER_DEFINITIONS", ())
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([BrowserProfile(id="university", name="University", purpose="University work")])
    return ApiServices(north_settings=settings, web_runtime=WebRuntime())


@pytest.mark.asyncio
async def test_setup_can_be_skipped_then_resumed_and_persisted(services):
    with bind_services(services):
        initial = await setup.get_setup()
        assert initial["status"] == "not_started"
        await setup.update_setup(SetupProgress(status="skipped", step=2))
        resumed = await setup.update_setup(SetupProgress(status="in_progress", step=3))
    assert resumed["step"] == 3
    assert resumed["status"] == "in_progress"
    assert "data_directory" not in resumed["profiles"][0]


@pytest.mark.asyncio
async def test_initial_timezone_detection_never_overwrites_saved_settings(services):
    with bind_services(services):
        detected = await update_settings(SettingsUpdate(timezone="America/Los_Angeles", initialize_timezone_only=True))
        repeated = await update_settings(SettingsUpdate(timezone="Asia/Kolkata", initialize_timezone_only=True))
        edited = await update_settings(SettingsUpdate(timezone="Asia/Kolkata"))
    assert detected.timezone == repeated.timezone == "America/Los_Angeles"
    assert edited.timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_profile_edit_invalidates_connection_evidence(services):
    services.web_runtime.browser_tests["university"] = {"status": "completed"}
    profile = services.north_settings.browser_profiles[0].model_copy(update={"purpose": "New purpose"})
    with bind_services(services):
        saved = await setup.update_browser_profiles(setup.ProfilesUpdate(profiles=[profile]))
    assert saved["tests"] == {}
    assert saved["profiles"][0]["purpose"] == "New purpose"


@pytest.mark.asyncio
async def test_disabled_or_missing_profiles_cannot_be_tested(services):
    services.north_settings.set_browser_profiles(
        [services.north_settings.browser_profiles[0].model_copy(update={"enabled": False})]
    )
    with bind_services(services), pytest.raises(HTTPException) as error:
        await setup.test_browser_profile("university")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_preflight_uses_real_central_approval_and_returns_without_blocking_dashboard(services):
    store = ApprovalStore()
    policy = approval_policy()
    approvals = Approvals(policy, UserInteraction(store, policy=policy))
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=services.north_settings)
    registry = ToolRegistry(auto_register=False, approvals=approvals)
    registry.register(tool)
    wired = services.replace(tool_registry=registry)
    command_result = _CommandResult(returncode=0, stdout='{"ok":true}', stderr="")
    profile = services.north_settings.browser_profiles[0]
    identity = _CommandResult(
        returncode=0, stdout=json.dumps({"ok": True, "result": str(profile.managed_directory / "Default")}), stderr=""
    )
    with (
        bind_services(wired),
        patch(
            "tools.universal.browser._run_chrome_agent", new_callable=AsyncMock, side_effect=[command_result, identity]
        ) as command,
        patch("tools.universal.browser.active_cdp_endpoint", return_value="ws://127.0.0.1:9222/devtools/browser/test"),
    ):
        result = await setup.test_browser_profile("university")
        duplicate = await setup.test_browser_profile("university")
        assert duplicate["test_id"] == result["test_id"]
        for _ in range(10):
            await asyncio.sleep(0)
            if store.pending():
                break
        [card] = store.pending()
        assert card.task_id == result["test_id"]
        assert result["status"] == "running"
        command.assert_not_called()
        store.resolve(card.id, ApprovalDecision.APPROVED)
        await drain()
        assert result["status"] == "completed"
        assert result["data"]["profile_verified"]
        assert not result["data"]["login_verified"]
        [navigation, check] = command.call_args_list
        assert "chrome://version" in navigation.args[0]
        assert "eval" in check.args[0]


def test_setup_routes_inherit_existing_session_and_csrf_protection(services):
    app = FastAPI()
    attach(app, services)
    app.include_router(session_router)
    app.include_router(router)
    client = TestClient(app, base_url="http://127.0.0.1")
    session = client.post("/web/session")
    assert session.status_code == 200
    assert client.get("/web/api/setup").status_code == 200
    assert client.post("/web/api/setup", json={"status": "skipped", "step": 0}).status_code == 403
    saved = client.post(
        "/web/api/setup", json={"status": "skipped", "step": 0}, headers={"X-North-CSRF": session.json()["csrf"]}
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "skipped"
