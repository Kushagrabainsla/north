"""Real Chrome and Safe-mode approvals, synthetic forms only; no model login.

NORTH_LIVE_BROWSER_DRAFTS=1 .venv/bin/pytest tests/live/test_browser_job_drafts_live.py -q -s
This checks tool integration, not autonomous model-driven job applications.
"""

from __future__ import annotations

import asyncio
import json
import os
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

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_BROWSER_DRAFTS") != "1",
    reason="set NORTH_LIVE_BROWSER_DRAFTS=1 to launch disposable Chrome",
)


async def test_verified_profile_can_prepare_two_forms_without_submitting_or_guessing(tmp_path):
    profile = BrowserProfile(
        id=f"livejobs-{uuid.uuid4().hex[:12]}",
        name="Synthetic University",
        purpose="Synthetic job drafts only",
        headed=False,
    )
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile])
    store = ApprovalStore()
    policy = approval_policy(ApprovalMode.SAFE)
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
    tool.approvals = Approvals(policy, UserInteraction(store, policy=policy))
    board = Board()
    port = _free_port()
    server, server_task = await _serve(board_app(board), port)
    base = f"http://127.0.0.1:{port}"

    async def execute(action, **params):
        assert action in {"preflight", "goto", "inspect", "fill", "assert", "eval"}
        if action == "goto":
            assert params["url"].startswith(f"{base}/")
        task = asyncio.create_task(
            tool.execute(
                ToolInput(
                    params={
                        "action": action,
                        "profile_id": profile.id,
                        "task_id": "synthetic-draft",
                        **params,
                    }
                )
            )
        )
        try:
            async with asyncio.timeout(60):
                while not task.done():
                    for card in store.pending():
                        assert card.agent == "browser" and card.task_id == "synthetic-draft"
                        store.resolve(card.id, ApprovalDecision.APPROVED)
                    await asyncio.sleep(0.02)
                output = await task
                assert output.success, output.error
                return output.data
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        checked = await execute("preflight")
        assert checked["profile_verified"] and not checked["login_verified"]
        assert board.requests == []
        for job in JOBS[:2]:
            await execute("goto", url=f"{base}/jobs/view/{job.job_id}/", stealth=False)
            posting = await execute("inspect")
            assert job.company in json.dumps(posting)
            await execute("goto", url=f"{base}/apply/{job.job_id}", stealth=False)
            for field, value in (("full_name", "Jane Doe"), ("email", "jane.doe@example.com")):
                selector = f'[name="{field}"]'
                await execute("fill", selector=selector, value=value)
                await execute("assert", selector=selector, assert_type="value", assert_condition="equals", value=value)
            unknowns = await execute(
                "eval",
                js=(
                    'JSON.stringify([document.querySelector("[name=salary]").value,'
                    'document.querySelector("[name=work_authorized]").value])'
                ),
            )
            assert json.loads(unknowns["result"]) == ["", ""]
        assert board.submissions == []
        assert all("otpToken" not in url for url in board.requests)
        assert not store.pending()
        print(
            json.dumps(
                {
                    "verified_profile": True,
                    "prepared_forms": 2,
                    "submissions": 0,
                    "unknowns_left_blank": True,
                    "approval_cards": len(store.all()),
                }
            )
        )
    finally:
        try:
            process = await asyncio.create_subprocess_exec(
                "chrome-agent",
                "--json",
                "--browser",
                profile.browser_name,
                "close",
                "--purge",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, "Disposable browser cleanup failed"
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=30)
