"""A real Claude Code and a real Codex asking north to fetch a page, through the real MCP route and approval layer.

The page is a stand-in (north's own fetch refuses loopback addresses), so what the agent can only learn by
calling `fetch_url` is a code word that is nowhere else. Skipped unless the CLI is installed and opted in.

NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_fetch_through_north_live.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from approval.models import ApprovalDecision
from coding_agents import Mode
from config.approval_mode import ApprovalMode
from orchestrator.coding_fetch import Page
from tests.live.support import start_gate
from tests.live.test_ask_north_live import BACKENDS, _guidance, _runner

URL = "https://docs.example.com/release-notes"
WORD = "PELICAN-4172"
TASK = (
    f"Plan nothing in the repository. Use the fetch_url tool to read {URL} and reply with the exact code word "
    "the page contains. If you cannot read the page, say so and do not guess."
)


class Pages:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str) -> Page:
        self.urls.append(url)
        return Page(url=URL, text=f"Release notes. The code word is {WORD}.")


async def _user_decides(gate, decision: ApprovalDecision) -> None:
    while True:
        for card in gate.store.pending():
            if card.title == "Coding agent - Fetch a page":
                gate.cards.append(card)
                gate.store.resolve(card.id, decision, chosen_option="Approve", decided_by="you")
                return
        await asyncio.sleep(0.05)


async def _run(backend, gate, tmp_path, repo, decision):
    answering = asyncio.create_task(_user_decides(gate, decision))
    try:
        report = await asyncio.wait_for(
            _runner(backend, gate, tmp_path).run(
                task_id="t-fetch",
                task=TASK,
                workspace=str(repo),
                mode=Mode.PLAN,
                backend=backend,
                guidance=_guidance(Mode.PLAN),
            ),
            timeout=240,
        )
        await asyncio.wait_for(answering, timeout=5)
        return report
    finally:
        answering.cancel()
        await gate.stop()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_the_user_approves_the_fetch_and_the_agent_reads_the_page(repo, tmp_path, backend) -> None:
    pages = Pages()
    gate = await start_gate(ApprovalMode.SAFE, pages=pages)

    report = await _run(backend, gate, tmp_path, repo, ApprovalDecision.APPROVED)

    assert report.outcome.ok, report.outcome.error
    assert WORD in report.outcome.text, report.outcome.text
    assert pages.urls == [URL]
    [card] = gate.cards
    assert URL in card.message and card.task_id == "t-fetch"


@pytest.mark.parametrize("backend", BACKENDS)
async def test_the_user_refuses_the_fetch_and_the_agent_never_gets_the_page(repo, tmp_path, backend) -> None:
    pages = Pages()
    gate = await start_gate(ApprovalMode.SAFE, pages=pages)

    report = await _run(backend, gate, tmp_path, repo, ApprovalDecision.REJECTED)

    assert report.outcome.ok, report.outcome.error
    assert WORD not in report.outcome.text, report.outcome.text
    assert pages.urls == []
