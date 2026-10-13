"""The pinned TaskWindow bridge, end to end, in a disposable Chrome for Testing profile.

Opt in with NORTH_LIVE_TASKWINDOW=1 and NORTH_TEST_CHROME=<Chrome for Testing binary>.
North installs and patches the pinned daemon, writes the per-profile extension
folder, and Chrome loads it the way the one-time setup does. Synthetic job board
only: no real sites, and nothing is ever submitted.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import approval_policy
from tests.live.jobs.fake_world import JOBS, Board, board_app
from tests.live.test_job_applications_live import _free_port, _serve
from tools.models import ToolInput
from tools.universal.browser import BrowserTool
from tools.universal.browser_taskwindow import TaskWindowBrowser

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_TASKWINDOW") != "1" or not os.environ.get("NORTH_TEST_CHROME"),
    reason="explicit TaskWindow opt-in and a Chrome for Testing binary required",
)


def _uid(snapshot: str, needle: str) -> str:
    line = next(line for line in snapshot.splitlines() if needle in line)
    return re.search(r"uid=(\S+)", line).group(1)


async def test_job_form_is_filled_in_a_background_task_tab_and_never_submitted(tmp_path):
    profile = BrowserProfile(
        id="university-live",
        name="Synthetic University",
        purpose="Synthetic forms only",
        context="existing",
        data_directory=str(tmp_path / "chrome"),
        profile_directory="Profile 3",
    )
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile])
    bridge = TaskWindowBrowser(tmp_path / "north")
    store = ApprovalStore()
    policy = approval_policy(ApprovalMode.SAFE)
    tool = BrowserTool(north_settings=settings, existing_browser=bridge)
    tool.approvals = Approvals(policy, UserInteraction(store, policy=policy))
    board = Board()
    port = _free_port()
    server, server_task = await _serve(board_app(board), port)
    base = f"http://127.0.0.1:{port}"
    task_id = f"live-{uuid.uuid4().hex}"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic resume")

    folder = (await bridge.setup(profile))["folder"]
    chrome = subprocess.Popen(  # noqa: S603 - test-only disposable browser
        [
            os.environ["NORTH_TEST_CHROME"],
            f"--user-data-dir={tmp_path / 'chrome'}",
            "--profile-directory=Profile 3",
            f"--load-extension={folder}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def execute(params):
        call = asyncio.create_task(
            tool.execute(ToolInput(params={**params, "profile_id": profile.id, "task_id": task_id}))
        )
        async with asyncio.timeout(180):
            while not call.done():
                for card in store.pending():
                    assert "Submit application" not in card.message, "North must never be asked to submit here"
                    store.resolve(card.id, ApprovalDecision.APPROVED)
                await asyncio.sleep(0.02)
        output = await call
        assert output.success, output.error
        return output.data

    # Chrome records only a real "Load unpacked" in the profile's preferences, not
    # a command-line load; the extension ID check below still runs for real.
    listing = patch(
        "tools.universal.browser_taskwindow.profiles_loading", return_value={"Profile 3": str(Path(folder).resolve())}
    )
    listing.start()
    try:
        checked = await execute({"action": "preflight"})
        assert checked["profile_verified"] and not checked["login_verified"]

        await execute({"action": "goto", "url": f"{base}/jobs/view/{JOBS[0].job_id}"})
        posting = await execute({"action": "inspect"})
        await execute({"action": "click", "uid": _uid(posting["snapshot"], 'link "Apply on company site"')})
        await execute({"action": "wait", "condition": "url", "pattern": "/apply/"})

        form = (await execute({"action": "inspect"}))["snapshot"]
        await execute({"action": "fill", "uid": _uid(form, 'textbox "Full name"'), "value": "Synthetic Applicant"})
        await execute({"action": "select", "selector": '[name="work_authorized"]', "value": "Yes"})
        await execute({"action": "upload", "selector": '[name="resume"]', "filename": str(resume)})
        for selector, expected in (('[name="full_name"]', "Synthetic Applicant"), ('[name="work_authorized"]', "Yes")):
            held = await execute(
                {
                    "action": "assert",
                    "selector": selector,
                    "assert_type": "value",
                    "assert_condition": "equals",
                    "value": expected,
                }
            )
            assert held["held"]
        attached = await execute({"action": "eval", "js": 'document.querySelector("[name=resume]").files[0].name'})
        assert attached["result"] == "resume.pdf"

        submit = _uid(form, 'button "Submit application"')
        card = await tool.describe(
            ToolInput(params={"action": "click", "uid": submit, "task_id": task_id, "profile_id": profile.id})
        )
        assert card.action.reaches_third_party and "in a form" in card.message

        shot = tmp_path / "form.png"
        await execute({"action": "screenshot", "filename": str(shot)})
        assert shot.read_bytes().startswith(b"\x89PNG")
        await execute({"action": "close"})
        assert not board.submissions
    finally:
        listing.stop()
        chrome.terminate()
        await bridge.aclose()
        server.should_exit = True
        await server_task


def test_extension_folder_stays_byte_identical_to_the_audited_copy(tmp_path):
    from utils.runtime_resources import resource_path

    profile = BrowserProfile(
        id="copy-check", name="Copy", purpose="Check", context="existing", data_directory=str(tmp_path)
    )
    from tools.universal.browser_taskwindow import _write_extension

    folder = TaskWindowBrowser(tmp_path).extension_dir(profile)
    _write_extension(folder, {"code": "X", "port": 1})
    audited = resource_path("taskwindow/extension")
    from tools.universal.browser_taskwindow import _same_files

    assert _same_files(audited, folder)
    assert {p.name for p in Path(folder).iterdir()} - {p.name for p in audited.iterdir()} == {
        "taskwindow-bootstrap.json"
    }
