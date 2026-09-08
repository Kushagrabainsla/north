"""Whether the browser tool can run, reported before an agent finds out the hard way.

The browser is the one capability that depends on a binary north cannot install
for the user, so its absence has to be visible in `north status` and on the
System page rather than surfacing as a failed task.
"""

from __future__ import annotations

import pytest

import tools.universal.browser as browser


@pytest.fixture
def nothing_installed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A machine with no chrome-agent anywhere, and no npx to fall back to."""
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)
    monkeypatch.setattr(browser.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(browser.Path, "is_file", lambda self: False)


def test_a_real_binary_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "_find_chrome_agent_binary", lambda: ["/usr/local/bin/chrome-agent"])
    state, detail = browser.browser_availability()
    assert state == "available"
    assert detail == "/usr/local/bin/chrome-agent"


def test_the_npx_fallback_is_not_reported_as_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """It works, but the first browse pays to download it - which is worth saying."""
    monkeypatch.setattr(browser, "_find_chrome_agent_binary", lambda: ["npx", "-y", "chrome-agent"])
    state, detail = browser.browser_availability()
    assert state == "on demand"
    assert "npx" in detail


def test_nothing_installed_says_how_to_fix_it(nothing_installed) -> None:
    state, detail = browser.browser_availability()
    assert state == "unavailable"
    # The point of the row: it carries the action, not just the absence.
    assert "cargo install chrome-agent" in detail
    assert "Chrome" in detail


def test_the_check_never_raises_on_a_bare_machine(nothing_installed) -> None:
    """It runs inside a status view, which must not fail because a tool is absent."""
    assert browser.browser_availability()[0] == "unavailable"
