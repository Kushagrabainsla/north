"""Several pages in one browser read, in parallel tabs (issue #83).

Model turns cost more than the browser: each one waits 2-6 s and resends about 14,000
input tokens. A task that read 3 LinkedIn profiles one by one spent about 66 s. One
`read` with `urls` opens up to three pages at a time, each in a tab of its own, and
gives the model all of them in one result: one call, one approval, one turn.
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import AsyncMock, Mock, patch

import pytest

from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import bind_approvals
from tools.models import ToolInput, ToolOutput
from tools.universal.browser import BrowserTool, _CommandResult
from tools.universal.browser_taskwindow import PARALLEL_READS, TaskWindowBrowser, page_chars

pytestmark = pytest.mark.usefixtures("chrome_installed")

URLS = [f"https://www.linkedin.com/in/person-{n}/" for n in range(5)]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


@pytest.fixture
def profile(tmp_path):
    return BrowserProfile(
        id="work", name="Work", purpose="Work", context="existing", data_directory=str(tmp_path / "chrome")
    )


async def test_the_bridge_reads_pages_three_at_a_time_in_tabs_of_their_own(tmp_path, profile) -> None:
    bridge = TaskWindowBrowser(tmp_path)
    bridge._session = lambda profile, slot: slot
    running, most, closed, asked = 0, 0, [], []

    async def open_page(profile, session, params):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        if params["url"].endswith("person-3/"):
            raise bridge_error("page did not load")
        return {"url": params["url"], "title": f"Title of {session}"}

    async def tool(profile, name, arguments, timeout=60):
        asked.append((name, arguments))
        return {"content": [{"type": "text", "text": f"text of {arguments['sessionToken']}"}]}

    from tools.universal.browser_taskwindow import BridgeInputError as bridge_error

    bridge._open = open_page
    bridge._tool = tool
    bridge._close = AsyncMock(side_effect=lambda profile, slot: closed.append(slot))

    pages = await bridge._read_many(profile, "t1", URLS)

    assert most == PARALLEL_READS
    assert [page["url"] for page in pages] == URLS  # in the order asked
    assert pages[0] == {"url": URLS[0], "title": "Title of t1#read0", "content": "text of t1#read0", "cut": False}
    assert pages[3] == {"url": URLS[3], "error": "page did not load"}
    assert sorted(closed) == [f"t1#read{n}" for n in range(5)]  # every tab of the batch, the failed one too
    assert all(arguments["maxLength"] == page_chars(5) for _, arguments in asked)


async def test_a_batch_runs_in_the_bridge_and_reads_as_one_result(tmp_path, profile) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile])
    pages = [{"url": URLS[0], "title": "One", "content": "first", "cut": True}, {"url": URLS[1], "error": "gone"}]
    native = Mock()
    native.call = AsyncMock(return_value=ToolOutput(success=True, data={"action": "read", "pages": pages}))
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(
        ToolInput(params={"action": "read", "urls": URLS[:2], "profile_id": "work", "task_id": "t"})
    )

    assert native.call.await_args.args[1]["urls"] == URLS[:2]
    text = tool.format_output(output.data)
    assert "## 1. One\nURL: https://www.linkedin.com/in/person-0/\n\nfirst" in text
    assert "read this URL alone for the rest" in text
    assert "## 2. https://www.linkedin.com/in/person-1/\nCould not read it: gone" in text


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"action": "read", "urls": [f"https://a.test/{n}" for n in range(11)]}, "at most 10"),
        ({"action": "goto", "urls": ["https://a.test/"]}, "Only action='read'"),
        ({"action": "read", "urls": ["https://a.test/", ""]}, "list of page addresses"),
    ],
)
async def test_a_batch_that_cannot_run_is_refused_before_any_card(params, problem) -> None:
    store = ApprovalStore()
    tool = bind_approvals(BrowserTool(binary_cmd=["chrome-agent"]), ApprovalMode.ASK, store=store)
    output = await tool.execute(ToolInput(params={**params, "task_id": "t"}))
    assert not output.success and problem in output.error
    assert not store.all(10)


async def test_a_batch_on_one_site_asks_once_and_covers_reading_that_site(tmp_path, profile) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile])
    native = Mock()
    native.call = AsyncMock(return_value=ToolOutput(success=True, data={"action": "read", "pages": []}))
    native.verify = AsyncMock(side_effect=RuntimeError("no page to look at in a test"))
    store = ApprovalStore()
    tool = bind_approvals(BrowserTool(north_settings=settings, existing_browser=native), ApprovalMode.ASK, store=store)

    async def asks(**params) -> str:
        running = asyncio.create_task(tool.execute(ToolInput(params={"profile_id": "work", "task_id": "t", **params})))
        while not (running.done() or store.pending()):
            await asyncio.sleep(0.01)
        message = store.pending()[0].message if store.pending() else ""
        if message:
            store.resolve(store.pending()[0].id, ApprovalDecision.APPROVED, decided_by="you")
        await running
        return message

    assert "Open and read 5 pages on linkedin.com" in await asks(action="read", urls=URLS)
    assert await asks(action="read", url="https://www.linkedin.com/in/someone-else/") == ""
    assert "on example.org, linkedin.com" in await asks(action="read", urls=[URLS[0], "https://example.org/"])


async def test_north_s_own_browser_reads_a_batch_page_by_page_in_one_call() -> None:
    def chrome_agent(command, timeout):
        if command[-1] == "read":
            return _CommandResult(0, json.dumps({"article": {"title": "T", "content": "x" * 50_000}}), "")
        return _CommandResult(0, '{"ok": true}', "")

    tool = BrowserTool(binary_cmd=["chrome-agent"])
    with patch("tools.universal.browser._run_chrome_agent", AsyncMock(side_effect=chrome_agent)):
        output = await tool.run(ToolInput(params={"action": "read", "urls": URLS[:2], "task_id": "t"}))

    assert [page["url"] for page in output.data["pages"]] == URLS[:2]
    assert all(len(page["content"]) == page_chars(2) and page["cut"] for page in output.data["pages"])
