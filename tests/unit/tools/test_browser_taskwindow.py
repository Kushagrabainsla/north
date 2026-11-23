"""The TaskWindow bridge proves the profile and never replays an action of unknown outcome."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from config.browser_profiles import BrowserProfile
from tools.universal import browser_taskwindow as bridge
from tools.universal.browser_taskwindow import (
    NORTH_EXTENSION_ID,
    TaskWindowBrowser,
    _ToolError,
    profiles_loading,
)


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


def preferences(root, directory, folder, extension_id=NORTH_EXTENSION_ID):
    path = root / directory / "Secure Preferences"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"location": 4, "path": str(folder)}
    path.write_text(json.dumps({"extensions": {"settings": {extension_id: entry}}}))


def text(value):
    return {"content": [{"type": "text", "text": value}]}


@pytest.fixture
def adapter(tmp_path, profile):
    browser = TaskWindowBrowser(tmp_path / "north")
    browser.verify = AsyncMock(return_value={"profile_verified": True, "login_verified": False, "connection_id": "c"})
    token = browser.state_dir(profile) / "daemon" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("key")
    browser._port = lambda _profile: 1
    return browser


def test_every_profile_copy_shares_one_id_and_keeps_the_audited_code(tmp_path):
    assert len(NORTH_EXTENSION_ID) == 32 and set(NORTH_EXTENSION_ID) <= set("abcdefghijklmnop")
    for name in ("a", "b"):
        bridge._write_extension(tmp_path / name, {"code": "X", "port": 1})
        manifest = json.loads((tmp_path / name / "manifest.json").read_text())
        assert bridge.extension_id_for_key(manifest["key"]) == NORTH_EXTENSION_ID
        assert bridge._same_files(bridge.resource_path("taskwindow/extension"), tmp_path / name)


def test_store_pairing_is_replaced_once_and_drift_is_refused(tmp_path):
    entry = tmp_path / "index.js"
    entry.write_text("before\n" + bridge._STORE_PAIRING + "after\n")
    bridge._apply_code_only_pairing(entry)
    assert bridge._STORE_PAIRING not in entry.read_text() and bridge._CODE_ONLY_PAIRING in entry.read_text()
    with pytest.raises(RuntimeError, match="differs from the audited copy"):
        bridge._apply_code_only_pairing(entry)


def test_profile_loading_reads_only_our_extension_and_its_folder(tmp_path):
    preferences(tmp_path, "Default", tmp_path / "x", extension_id="other")
    preferences(tmp_path, "Profile 3", tmp_path / "ours")
    assert profiles_loading(tmp_path, NORTH_EXTENSION_ID) == {"Profile 3": str((tmp_path / "ours").resolve())}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connected", "extension", "holders", "error"),
    [
        (False, None, ["Profile 3"], "not connected"),
        (True, "someone-else", ["Profile 3"], "different extension"),
        (True, "ours", ["Profile 3", "Default"], "not only in Profile 3"),
        (True, "ours", ["Default"], "not only in Profile 3"),
    ],
)
async def test_verify_refuses_anything_but_our_extension_in_only_this_profile(
    tmp_path, profile, connected, extension, holders, error
):
    browser = TaskWindowBrowser(tmp_path / "north")
    health = {
        "ok": True,
        "extensionConnected": connected,
        "extensionId": NORTH_EXTENSION_ID if extension == "ours" else extension,
    }
    browser._ensure_daemon = AsyncMock(return_value=(1, health))
    for directory in holders:
        preferences(tmp_path / "chrome", directory, browser.extension_dir(profile))
    with pytest.raises(bridge.BridgeInputError, match=error):
        await browser.verify(profile, wait=False)


@pytest.mark.asyncio
async def test_verify_accepts_our_extension_in_this_profile(tmp_path, profile):
    browser = TaskWindowBrowser(tmp_path / "north")
    browser._ensure_daemon = AsyncMock(
        return_value=(7, {"ok": True, "extensionConnected": True, "extensionId": NORTH_EXTENSION_ID})
    )
    preferences(tmp_path / "chrome", "Profile 3", browser.extension_dir(profile))
    preferences(tmp_path / "chrome", "Default", tmp_path / "another-profiles-own-folder")
    identity = await browser.verify(profile, wait=False)
    assert identity["profile_verified"] and not identity["login_verified"]


@pytest.mark.asyncio
async def test_uncertain_page_action_blocks_the_next_one_until_close(adapter, profile):
    adapter._tool = AsyncMock(side_effect=_ToolError("timed out", uncertain=True))
    first = await adapter.call(profile, {"action": "fill", "task_id": "t", "uid": "n1", "value": "x"})
    assert not first.success
    adapter._tool = AsyncMock(return_value=text("{}"))
    second = await adapter.call(profile, {"action": "fill", "task_id": "t", "uid": "n1", "value": "x"})
    assert not second.success and "unknown outcome" in second.error
    adapter._tool.assert_not_called()
    status = await adapter.call(profile, {"action": "preflight", "task_id": "t"})
    assert status.data["uncertain_action"] is True


@pytest.mark.asyncio
async def test_reported_failure_does_not_leave_an_unknown_outcome(adapter, profile):
    adapter._tool = AsyncMock(side_effect=_ToolError("no element matches selector", uncertain=False))
    assert not (await adapter.call(profile, {"action": "fill", "task_id": "t", "selector": "#x", "value": "1"})).success
    adapter._tool = AsyncMock(return_value=text("{}"))
    assert (await adapter.call(profile, {"action": "fill", "task_id": "t", "selector": "#x", "value": "1"})).success


@pytest.mark.asyncio
async def test_click_is_refused_when_the_element_changed_since_inspect(adapter, profile):
    adapter._seen[(profile.id, "t")] = {"n4": ("button", '"Next"')}
    spot = {"found": True, "reachable": True, "x": 5, "y": 6, "role": "button", "name": '"Submit application"'}
    adapter._tool = AsyncMock(return_value=text(json.dumps(spot)))
    output = await adapter.call(profile, {"action": "click", "task_id": "t", "uid": "n4"})
    assert not output.success and "changed since it was inspected" in output.error
    assert all(call.args[1] != "computer" for call in adapter._tool.call_args_list)


@pytest.mark.asyncio
async def test_click_lands_on_the_inspected_element(adapter, profile):
    adapter._seen[(profile.id, "t")] = {"n4": ("button", '"Next"')}
    spot = {"found": True, "reachable": True, "x": 5, "y": 6, "role": "button", "name": '"Next"'}
    # Two looks that agree (the element is still), then the click.
    adapter._tool = AsyncMock(side_effect=[text(json.dumps(spot)), text(json.dumps(spot)), text("clicked")])
    output = await adapter.call(profile, {"action": "click", "task_id": "t", "uid": "n4"})
    assert output.success
    tool, arguments = adapter._tool.call_args_list[-1].args[1:3]
    assert tool == "computer" and (arguments["x"], arguments["y"]) == (5, 6) and arguments["sessionToken"]


@pytest.mark.asyncio
async def test_open_creates_the_task_tab_once_then_navigates(adapter, profile):
    page = text(json.dumps({"url": "https://a.test/", "title": "A"}))
    adapter._tool = AsyncMock(
        side_effect=[text("no tabs in this session's groups yet"), text("opened"), text("true"), page]
    )
    await adapter.call(profile, {"action": "goto", "task_id": "t", "url": "https://a.test/"})
    assert adapter._tool.call_args_list[1].args[1] == "tabs_create"
    assert adapter._tool.call_args_list[1].args[2]["longRunning"] is True
    adapter._tool = AsyncMock(side_effect=[text("12. A — https://a.test/"), text("now at"), page])
    await adapter.call(profile, {"action": "goto", "task_id": "t", "url": "https://a.test/b"})
    assert adapter._tool.call_args_list[1].args[1] == "navigate"


@pytest.mark.asyncio
async def test_upload_sends_the_file_bytes_to_the_chosen_input(adapter, profile, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1")
    adapter._tool = AsyncMock(return_value=text("attached"))
    output = await adapter.call(profile, {"action": "upload", "task_id": "t", "uid": "n9", "filename": str(resume)})
    assert output.success and output.data["uploaded"] == "resume.pdf"
    arguments = adapter._tool.call_args.args[2]
    assert arguments["selector"] == '[data-north-uid="n9"]'
    assert arguments["files"][0]["mimeType"] == "application/pdf" and arguments["files"][0]["data"] == "JVBERi0x"


@pytest.mark.asyncio
async def test_tasks_get_separate_sessions_that_survive_a_restart(adapter, profile, tmp_path):
    again = TaskWindowBrowser(tmp_path / "north")
    assert adapter._session(profile, "a") != adapter._session(profile, "b")
    assert adapter._session(profile, "a") == again._session(profile, "a")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_page_scripts_parse():
    scripts = [
        bridge._SNAPSHOT_SCRIPT % {"limit": 10},
        bridge._LOCATE_SCRIPT % {"selector": json.dumps("#a")},
        bridge._TABLES_SCRIPT % {"limit": 5, "query": json.dumps("")},
        bridge._ASSERT_SCRIPT % {"kind": '"text"', "condition": '"contains"', "expected": '"x"', "selector": '""'},
        bridge._WAIT_SCRIPT % {"condition": '"text"', "pattern": '"x"'},
        bridge._PAGE_STATE_SCRIPT % {"names": '["Ada Lovelace"]'},
    ]
    for script in scripts:
        subprocess.run(["node", "-e", "new Function(process.argv[1])", script], check=True)


def test_reconnect_refreshes_the_code_without_replacing_a_matching_copy(tmp_path):
    folder = tmp_path / "extension"
    bridge._write_extension(folder, {"code": "ONE", "port": 1})
    manifest = folder / "manifest.json"
    loaded = manifest.stat().st_ino
    bridge._write_extension(folder, {"code": "TWO", "port": 1})
    assert manifest.stat().st_ino == loaded
    assert json.loads((folder / "taskwindow-bootstrap.json").read_text())["code"] == "TWO"
    manifest.write_text("{}")
    bridge._write_extension(folder, {"code": "THREE", "port": 1})
    assert json.loads(manifest.read_text())["name"] == "TaskWindow"


@pytest.mark.asyncio
async def test_page_reads_retry_a_navigation_race_but_actions_do_not(adapter, profile, monkeypatch):
    monkeypatch.setattr(bridge.asyncio, "sleep", AsyncMock())
    race = _ToolError("Error: Debugger is not attached to the tab with id: 7.", uncertain=False)
    adapter._tool = AsyncMock(side_effect=[race, text('{"held": true}')])
    assert await adapter._script(profile, "s", "1") == {"held": True}
    adapter._tool = AsyncMock(side_effect=race)
    output = await adapter.call(profile, {"action": "press", "task_id": "t", "value": "Enter"})
    assert not output.success and adapter._tool.await_count == 1


@pytest.mark.asyncio
async def test_front_goes_back_to_the_users_app_only_when_chrome_jumped(tmp_path, monkeypatch):
    chrome = BrowserProfile(
        id="p", name="P", purpose="x", context="existing",
        data_directory=str(tmp_path / "Library/Application Support/Google/Chrome"),
    )  # fmt: skip
    fronts = iter(["com.user.App", "com.user.App", "com.google.Chrome"])
    monkeypatch.setattr(bridge, "frontmost_app", lambda: next(fronts, "com.google.Chrome"))
    restored = []
    monkeypatch.setattr(bridge, "activate", restored.append)
    async with bridge._front_kept(chrome):
        pass
    assert restored == ["com.user.App"]

    monkeypatch.setattr(bridge, "frontmost_app", lambda: "com.user.App")
    restored.clear()
    async with bridge._front_kept(chrome):
        pass
    assert restored == []


@pytest.mark.asyncio
async def test_a_key_press_goes_to_the_element_the_call_names(adapter, profile):
    """Issue #83: Enter after filling a search box went to whatever had focus, so the search never ran."""
    adapter._tool = AsyncMock(side_effect=[text("null"), text("pressed")])
    output = await adapter.call(profile, {"action": "press", "task_id": "t", "selector": "#q", "value": "Enter"})
    assert output.success
    (focus, focus_args), (press, press_args) = [call.args[1:3] for call in adapter._tool.call_args_list]
    assert focus == "javascript_execute" and '"#q"' in focus_args["code"] and ".focus()" in focus_args["code"]
    assert press == "computer"


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "sent"), [("Enter", {"action": "type", "text": "\n"}), ("Tab", {"key": "Tab"})])
async def test_enter_is_sent_as_typed_so_it_submits_a_form(adapter, profile, key, sent):
    """TaskWindow's "key" sends Enter with no "\\r", and a form submits only on an Enter that carries it.

    Wikipedia's search box never searched. Typed, a line break is a real Enter with "\\r".
    """
    adapter._tool = AsyncMock(side_effect=[text("pressed")])
    output = await adapter.call(profile, {"action": "press", "task_id": "t", "value": key})
    assert output.success
    name, arguments = adapter._tool.call_args.args[1:3]
    assert name == "computer" and sent.items() <= arguments.items()


@pytest.mark.asyncio
async def test_connected_asks_the_running_bridge_and_starts_none(tmp_path, profile):
    browser = TaskWindowBrowser(tmp_path / "north")
    assert await browser.connected(profile) is False  # no bridge has run for it: nothing is started
    port = browser.state_dir(profile) / "port"
    port.parent.mkdir(parents=True)
    port.write_text("4321")
    browser._health = AsyncMock(return_value={"ok": True, "extensionConnected": True})
    assert await browser.connected(profile) is True
    browser._health.assert_awaited_once_with(4321)


def test_an_old_extension_copy_is_brought_up_to_date_in_place(tmp_path):
    """Both profiles ran without evaluate.js five days after it shipped: the copy was written only at connect."""
    folder = tmp_path / "copy"
    bridge._write_extension(folder, {"code": "KEEP", "port": 1})
    (folder / "tools" / "evaluate.js").unlink()
    (folder / "tools" / "javascript.js").write_text("// old code")
    (folder / "tools" / "gone.js").write_text("// a file the audited copy no longer has")
    audited = bridge.resource_path("taskwindow/extension")

    assert bridge._refresh_extension_files(folder) is True

    assert bridge._same_files(audited, folder)
    assert not (folder / "tools" / "gone.js").exists()
    assert json.loads((folder / "taskwindow-bootstrap.json").read_text())["code"] == "KEEP"  # still paired
    assert bridge._refresh_extension_files(folder) is False  # nothing to do the second time


@pytest.mark.asyncio
async def test_an_updated_copy_is_reloaded_once_and_waits_for_the_extension(tmp_path, profile, monkeypatch):
    browser = TaskWindowBrowser(tmp_path / "north")
    token = browser.state_dir(profile) / "daemon" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("key")
    monkeypatch.setattr(bridge.asyncio, "sleep", AsyncMock())
    changed = iter([True, False])
    monkeypatch.setattr(bridge, "_refresh_extension_files", lambda folder: next(changed))
    posted = []

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers):
            posted.append((url, headers["Authorization"]))
            return bridge.httpx.Response(200, request=bridge.httpx.Request("POST", url))

    monkeypatch.setattr(bridge.httpx, "AsyncClient", _Client)
    back = {"extensionConnected": True, "extensionId": NORTH_EXTENSION_ID, "extensionVersion": "new"}
    browser._health = AsyncMock(side_effect=[{"extensionConnected": False}, back])

    assert await browser._bring_extension_up_to_date(profile, 5, {}) == back
    assert posted == [("http://127.0.0.1:5/extension/reload", "Bearer key")]
    assert profile.id in browser._extension_current


@pytest.mark.asyncio
async def test_a_click_waits_until_its_element_is_drawn_uncovered_and_still(adapter, profile, monkeypatch):
    """23 clicks ended "not found" or "covered" on their first look (2026-11-10 to 11-22)."""
    monkeypatch.setattr(bridge.asyncio, "sleep", AsyncMock())
    looks = [
        {"found": False},
        {"found": True, "reachable": False, "x": 5, "y": 5, "role": "button", "name": '"Go"'},
        {"found": True, "reachable": True, "x": 10, "y": 40, "role": "button", "name": '"Go"'},
        {"found": True, "reachable": True, "x": 10, "y": 20, "role": "button", "name": '"Go"'},
        {"found": True, "reachable": True, "x": 10, "y": 20, "role": "button", "name": '"Go"'},
    ]
    adapter._script = AsyncMock(side_effect=looks)
    adapter._tool = AsyncMock(return_value=text("clicked"))

    output = await adapter.call(profile, {"action": "click", "task_id": "t", "selector": "#go"})

    assert output.success and output.data["x"] == 10 and output.data["y"] == 20  # where it stopped moving
    assert adapter._script.await_count == 5


@pytest.mark.asyncio
async def test_a_click_on_an_element_that_never_shows_still_fails_and_says_so(adapter, profile, monkeypatch):
    monkeypatch.setattr(bridge, "_CLICKABLE_SECONDS", 0)
    adapter._script = AsyncMock(return_value={"found": False})
    adapter._tool = AsyncMock()

    output = await adapter.call(profile, {"action": "click", "task_id": "t", "selector": "#gone"})

    assert not output.success and "not found" in output.error
    adapter._tool.assert_not_awaited()  # nothing was clicked
