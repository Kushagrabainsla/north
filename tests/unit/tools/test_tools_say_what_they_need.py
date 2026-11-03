"""A tool or flow that needs something this machine lacks says so, and is not offered as the way forward.

Issue #76: the browser reported a name that does not resolve as "Security policy
blocked navigation" (W13); take_screenshot, on a host with no display, failed every
call while agents kept offering it (W11); and the developer-only nightly-check flow
was listed ACTIVE on every install, where it could only fail (M23).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import tools.universal.browser as browser
from flows import FlowRegistry
from flows.exceptions import FlowNotFoundError
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


def test_a_withheld_tool_is_not_offered_and_says_why(tmp_path: Path) -> None:
    registry = ToolRegistry(auto_register=True, learned_dir=tmp_path / "learned")
    assert "take_screenshot" in registry.all_tool_names()

    registry.withhold("take_screenshot", "Screen capture is not available: north is running with no display here.")
    registry._dir_mtimes.clear()  # force the next reload to rescan every tool directory
    registry.reload()

    assert "take_screenshot" not in registry.all_tool_names()
    assert registry.unavailable() == {
        "take_screenshot": "Screen capture is not available: north is running with no display here."
    }


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
