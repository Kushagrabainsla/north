"""Approved synthetic forms in task-owned windows of an explicitly chosen Chrome.

Opt in with NORTH_LIVE_EXISTING_CHROME=1, NORTH_EXISTING_CHROME_DIR and
NORTH_EXISTING_CHROME_PROFILE. No real job sites, credentials or resume uploads.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

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
from tools.universal.browser_native import ExistingChrome

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_EXISTING_CHROME") != "1",
    reason="explicit existing-Chrome opt-in required",
)


@pytest.mark.parametrize("trial", range(3))
async def test_two_task_windows_reuse_resume_and_close_without_submitting(tmp_path, trial):
    profile = BrowserProfile(
        id="university-live-probe",
        name="Synthetic University test",
        purpose="Synthetic forms only",
        context="existing",
        data_directory=os.environ["NORTH_EXISTING_CHROME_DIR"],
        profile_directory=os.environ["NORTH_EXISTING_CHROME_PROFILE"],
    )
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile])
    store = ApprovalStore()
    policy = approval_policy(ApprovalMode.SAFE)
    native = ExistingChrome(tmp_path / "north")
    tool = BrowserTool(north_settings=settings, existing_browser=native)
    tool.approvals = Approvals(policy, UserInteraction(store, policy=policy))
    board = Board()
    port = _free_port()
    server, server_task = await _serve(board_app(board), port)
    base = f"http://127.0.0.1:{port}"
    task_ids = [f"native-{uuid.uuid4().hex}", f"native-{uuid.uuid4().hex}"]
    window_ids = {}

    async def invoke(task_id, params):
        assert params["action"] in {"preflight", "goto", "inspect", "fill", "assert", "eval", "close"}
        if "url" in params:
            assert params["url"].startswith(f"{base}/")
        call = asyncio.create_task(
            tool.execute(
                ToolInput(
                    params={
                        **params,
                        "profile_id": profile.id,
                        "task_id": task_id,
                    }
                )
            )
        )
        try:
            async with asyncio.timeout(180):
                while not call.done():
                    for card in store.pending():
                        assert card.agent == "browser" and card.task_id in task_ids
                        store.resolve(card.id, ApprovalDecision.APPROVED)
                    await asyncio.sleep(0.02)
                output = await call
            return output
        finally:
            if not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    async def execute(task_id, params):
        output = await invoke(task_id, params)
        assert output.success, output.error
        if "window_id" in output.data:
            previous = window_ids.setdefault(task_id, output.data["window_id"])
            assert previous == output.data["window_id"]
        return output.data

    try:
        for task_id in task_ids:
            checked = await execute(task_id, {"action": "preflight"})
            assert checked["profile_verified"] and not checked["login_verified"]
        assert window_ids[task_ids[0]] != window_ids[task_ids[1]]
        assert not board.requests
        for form_round in range(3):
            for task_id in task_ids:
                await execute(task_id, {"action": "goto", "url": f"{base}/apply/{JOBS[0].job_id}"})
                inspected = await execute(task_id, {"action": "inspect"})
                name_nodes = [line for line in inspected["snapshot"].splitlines() if 'textbox "Full name"' in line]
                assert name_nodes, inspected["snapshot"]
                name_node = name_nodes[0]
                uid = re.search(r"uid=(\S+)", name_node).group(1)
                value = f"Synthetic {task_id} round {form_round}"
                await execute(task_id, {"action": "fill", "uid": uid, "value": value})
                submit_node = next(
                    line for line in inspected["snapshot"].splitlines() if 'button "Submit application"' in line
                )
                submit_uid = re.search(r"uid=(\S+)", submit_node).group(1)
                request = await tool.describe(
                    ToolInput(
                        params={"action": "click", "uid": submit_uid, "task_id": task_id, "profile_id": profile.id}
                    )
                )
                assert request.action.reaches_third_party
                assert "in a form" in request.message
                await execute(
                    task_id,
                    {
                        "action": "assert",
                        "selector": '[name="full_name"]',
                        "assert_type": "value",
                        "assert_condition": "equals",
                        "value": value,
                    },
                )
                for field in ("salary", "work_authorized"):
                    await execute(
                        task_id,
                        {
                            "action": "assert",
                            "selector": f'[name="{field}"]',
                            "assert_type": "value",
                            "assert_condition": "equals",
                            "value": "",
                        },
                    )
        # A paused run keeps its page, and a private-worker restart restores it.
        await tool.aclose()
        await execute(
            task_ids[0],
            {
                "action": "assert",
                "selector": '[name="full_name"]',
                "assert_type": "value",
                "assert_condition": "contains",
                "value": "round 2",
            },
        )
        mutation = {
            "action": "eval",
            "js": 'const el=document.querySelector("[name=full_name]"); '
            'el.value=String((Number(el.value)||0)+1); throw new Error("Synthetic post-mutation exception");',
        }
        first = await invoke(task_ids[0], mutation)
        assert not first.success and "Synthetic post-mutation exception" in first.error
        await tool.aclose()
        replay = await invoke(task_ids[0], mutation)
        assert not replay.success and "unknown outcome" in replay.error
        await execute(
            task_ids[0],
            {
                "action": "assert",
                "selector": '[name="full_name"]',
                "assert_type": "value",
                "assert_condition": "equals",
                "value": "1",
            },
        )
        assert not board.submissions
        assert not store.pending()
        print(
            json.dumps(
                {
                    "task_windows": 2,
                    "trial": trial,
                    "fill_rounds": 6,
                    "worker_restart_preserved_draft": True,
                    "unknown_effect_not_replayed": True,
                    "submission_classified_without_clicking": True,
                    "submissions": 0,
                    "unknowns_left_blank": True,
                    "approval_cards": len(store.all()),
                }
            )
        )
    finally:
        try:
            for task_id in task_ids:
                await execute(task_id, {"action": "close"})
        finally:
            await tool.aclose()
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=30)
