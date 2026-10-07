"""The job-application flow end to end, against a fake inbox and a fake job board, with north's real model.

From a LinkedIn job-alert email to filled-in applications the user submits themselves: north reads the
alert through Gmail (a stand-in MCP server), picks the jobs that fit, fills each application on the board
in an isolated browser, leaves one card per job without waiting, and remembers what it handled. It runs
in the user's own approval mode ("safe"), with a stand-in user approving any card that blocks, so the test
also counts how often a real run would stop to ask.

Never against real sites. It spends a little of the model the user has north routed to.

NORTH_LIVE_JOBS=1 .venv/bin/python -m pytest tests/live/test_job_applications_live.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

import httpx
import pytest

from tests.live.jobs.fake_world import JOBS, SECRET_TOKEN, Board, board_app, inbox_json, write_resume

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_JOBS") != "1" or shutil.which("chrome-agent") is None,
    reason="set NORTH_LIVE_JOBS=1 with chrome-agent installed",
)

REAL_HOME = Path.home() / ".north"
SKILL = Path(__file__).parent / "jobs" / "skill" / "drafting-applications-from-job-alerts"
FAKE_MCP = Path(__file__).parents[1] / "unit" / "mcp" / "fake_mcp_server.py"
RUN_TIMEOUT_S = 1500


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.1)
    return server, task


def _home(home: Path, board_base: str, workspace: Path) -> None:
    """A throwaway north home: the user's model login and settings, the fake inbox, the skill and a flow."""
    (home / "credentials").mkdir(parents=True)
    shutil.copy2(REAL_HOME / "credentials" / "openai_codex.json", home / "credentials" / "openai_codex.json")
    settings = json.loads((REAL_HOME / "settings.json").read_text(encoding="utf-8"))
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (home / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "gmail": {
                        "command": sys.executable,
                        "args": [str(FAKE_MCP)],
                        "env": {"FAKE_MCP_MESSAGES": inbox_json(board_base)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    shutil.copytree(SKILL, home / "skills" / SKILL.name)
    installed = home / "skills" / SKILL.name / "SKILL.md"  # as it is once the user has activated it
    installed.write_text(installed.read_text(encoding="utf-8").replace("status: candidate", "status: active"))
    resume = write_resume(workspace / "Jane_Doe_Resume.pdf")
    flow = home / "flows" / "job-alert-applications"
    flow.mkdir(parents=True)
    (flow / "FLOW.yaml").write_text(
        "name: job-alert-applications\n"
        "description: Draft applications for new LinkedIn job alerts; the user submits them.\n"
        "source: learned\nstatus: candidate\ndomains: [general]\nsteps:\n"
        "- name: draft-applications\n  skill: drafting-applications-from-job-alerts\n"
        "  instructions: Draft today's applications.\n  approval: on_mutation\n  inputs:\n"
        f"    resume_path: {resume}\n"
        "    preferences: New grad software engineer roles in the US. Authorized to work in the US.\n"
        "    max_jobs: 2\n    browser_context: isolated\n    context_confirmed: true\n",
        encoding="utf-8",
    )


@pytest.fixture
async def north(monkeypatch, tmp_path):
    from config.settings import settings

    home, workspace = tmp_path / "home", tmp_path / "work"
    workspace.mkdir()
    board = Board()
    board_port, north_port = _free_port(), _free_port()
    board_base = f"http://127.0.0.1:{board_port}"
    _home(home, board_base, workspace)
    monkeypatch.setenv("NORTH_HOME", str(home))
    monkeypatch.setattr(settings, "north_home", home)
    monkeypatch.setattr(settings, "north_env", "test")
    monkeypatch.setattr(settings, "north_workspace", str(workspace))
    monkeypatch.setattr(settings, "north_orchestrator_url", f"http://127.0.0.1:{north_port}")

    from orchestrator.app import app

    board_server, board_task = await _serve(board_app(board), board_port)
    north_server, north_task = await _serve(app, north_port)
    from config.security import load_secret

    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{north_port}", headers={"X-North-Secret": load_secret()}, timeout=60
    )
    yield client, board, home
    await client.aclose()
    for server, task in ((north_server, north_task), (board_server, board_task)):
        server.should_exit = True
        await task


async def _run_test_flow(client: httpx.AsyncClient) -> tuple[dict, list[str]]:
    """Start a test run and approve every blocking card until it ends, like a user at the screen."""
    started = (await client.post("/web/api/flow-definitions/job-alert-applications/runs", json={"mode": "test"})).json()
    assert "run_id" in started, started
    asked: list[str] = []
    deadline = asyncio.get_running_loop().time() + RUN_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        for card in (await client.get("/web/api/approvals")).json():
            if card.get("blocking") and card.get("status") == "pending":
                asked.append(card.get("title", ""))
                await client.post(
                    "/orchestrator/approval/respond", json={"card_id": card["id"], "decision": "approved"}
                )
        runs = (await client.get("/web/api/flow-runs", params={"flow": "job-alert-applications"})).json()
        run = next((r for r in runs if started["run_id"] in (r.get("run_id"), r.get("id"))), None)
        if run and run.get("status") not in ("running", "waiting"):
            return run, asked
        await asyncio.sleep(3)
    pytest.fail(f"the run did not end in {RUN_TIMEOUT_S}s; it stopped to ask {len(asked)} times: {asked}")


async def _left_for_user(client: httpx.AsyncClient) -> list[dict]:
    """Every non-blocking card: the work north left for the user, decided or not."""
    return [c for c in (await client.get("/web/api/approvals")).json() if not c.get("blocking")]


async def test_a_job_alert_becomes_filled_applications_the_user_submits(north) -> None:
    client, board, home = north
    for verb in ("goto", "fill", "type", "select"):  # what the user switches on in safe mode's list
        switched = await client.patch(f"/web/api/unattended/rules/browser:{verb}", json={"enabled": True})
        assert switched.status_code == 200, switched.text

    run, asked = await _run_test_flow(client)
    cards = [
        c for c in (await client.get("/web/api/approvals", params={"status": "all"})).json() if not c.get("blocking")
    ]
    print(f"\nrun: {run.get('status')} {run.get('error', '')}\nblocking cards ({len(asked)}): {asked}")
    print("left for the user:", [c.get("title") for c in cards])

    assert run["status"] == "completed", run
    assert board.submissions == [], "north must never submit an application"
    assert not [url for url in board.requests if SECRET_TOKEN in url], "a link with the sign-in token was opened"
    titles = " ".join(c.get("title", "") for c in cards)
    assert "Acme Robotics" in titles and "Northwind Labs" in titles, titles
    assert "Globex" not in titles, "the senior role does not fit a new grad"
    values = [str(f.get("value", "")) for c in cards for f in c.get("fields", [])]
    assert "jane.doe@example.com" in values and "Jane Doe" in values, values
    assert not [v for v in values if re.fullmatch(r"\$?\s?\d{2,3}[,.]?\d{3}(\s?(USD|/yr))?", v.strip())], (
        f"a salary was invented: {values}"
    )
    [seen_file] = list(home.parent.rglob("seen.json"))  # wherever this run's "~/.north" pointed
    seen = seen_file.read_text(encoding="utf-8")
    assert all(job.job_id in seen for job in JOBS), seen

    again, _ = await _run_test_flow(client)
    after = [
        c for c in (await client.get("/web/api/approvals", params={"status": "all"})).json() if not c.get("blocking")
    ]
    assert again["status"] == "completed" and len(after) == len(cards), "the same jobs were offered twice"
