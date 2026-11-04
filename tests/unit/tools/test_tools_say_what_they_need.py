"""A tool or flow that needs something this machine lacks says so, and is not offered as the way forward.

Issue #76: the browser reported a name that does not resolve as "Security policy
blocked navigation" (W13); take_screenshot, on a host with no display, failed every
call while agents kept offering it, and so did the browser with no Chrome (W11); and
the developer-only nightly-check flow was listed ACTIVE on every install, where it
could only fail (M23).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import tools.universal.browser as browser
from flows import FlowRegistry
from flows.exceptions import FlowNotFoundError
from tools.models import ToolInput
from tools.registry import ToolRegistry


def test_a_name_that_does_not_resolve_is_a_network_failure_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_dns(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)

    why = browser._unsafe_url_reason("goto", {"url": "https://www.google.com/"})

    assert why.startswith("Network failure, not a security refusal: Could not resolve host 'www.google.com'")
    assert "Security policy" not in why


def test_a_private_address_is_still_refused_by_policy() -> None:
    assert browser._unsafe_url_reason("goto", {"url": "http://10.0.0.5/admin"}).startswith(
        "Security policy blocked navigation"
    )


def test_a_tool_is_offered_only_while_what_it_needs_is_there(tmp_path: Path) -> None:
    registry = ToolRegistry(auto_register=True, learned_dir=tmp_path / "learned")
    display = {"why": "north is running with no display here"}
    registry.require("take_screenshot", lambda: display["why"])

    assert "take_screenshot" not in registry.all_tool_names()
    assert "take_screenshot" not in {tool.name for tool in registry.available_tools(auto_reload=False)}
    assert registry.unavailable() == {
        "take_screenshot": "take_screenshot cannot work on this machine: north is running with no display here"
    }
    assert registry.get("take_screenshot").name == "take_screenshot"  # still found, to explain a direct call

    display["why"] = ""  # what it needs is here now: no restart

    assert "take_screenshot" in registry.all_tool_names() and registry.unavailable() == {}


async def test_with_no_chrome_the_browser_is_not_offered_and_a_call_raises_no_card(monkeypatch, tmp_path) -> None:
    """Asked how else to show a page, agents offered "your browser profiles" on a machine with no Chrome."""
    monkeypatch.setattr(browser, "_find_chrome", lambda: None)
    registry = ToolRegistry(auto_register=False)
    registry.register(browser.BrowserTool(binary_cmd=["chrome-agent"]))
    registry.require("browser", browser.browser_unavailable)

    out = await registry.get("browser").execute(ToolInput(params={"action": "goto", "url": "https://example.org/"}))

    assert "browser" not in registry.all_tool_names()
    assert "no Chrome or Chromium was found" in registry.unavailable()["browser"]
    assert not out.success and out.error.startswith("The browser cannot work here: no Chrome or Chromium")


def test_a_flow_is_offered_only_while_what_it_needs_is_there(tmp_path: Path) -> None:
    builtin = Path(__file__).parents[3] / "resources" / "builtin-flows"
    flows = FlowRegistry(builtin_dir=builtin, learned_dir=tmp_path / "flows")
    registered = {"checkout": ""}
    flows.require("nightly-check", lambda: "" if registered["checkout"] else "no north project folder is registered")

    assert "nightly-check" not in flows.names()
    with pytest.raises(FlowNotFoundError, match="not available here: no north project folder"):
        flows.get("nightly-check")

    registered["checkout"] = "/home/me/north"  # scripts/install_smoke_schedule.sh ran: no restart needed

    assert "nightly-check" in flows.names() and flows.get("nightly-check").name == "nightly-check"
