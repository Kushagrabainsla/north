"""Every tab north drives renders at a desktop size, whatever the size of the window it is in.

The user sizes and places north's background Chrome window to fit their own work. At
848 px wide Wikipedia hid its search box, so every search failed (2026-11-22). When the
extension attaches to a tab, it sets the tab's screen to 1280 x 900 desktop. This runs
the extension's own cdp.js under Node with a stand-in `chrome`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


@pytest.fixture(scope="module")
def sent() -> list[dict]:
    script = Path(__file__).with_name("attach_viewport.mjs")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


def test_an_attached_tab_gets_a_desktop_screen_of_the_agent_window_s_size(sent) -> None:
    [viewport] = [command for command in sent if command["method"] == "Emulation.setDeviceMetricsOverride"]
    assert viewport["tabId"] == 7
    assert viewport["params"] == {"width": 1280, "height": 900, "deviceScaleFactor": 0, "mobile": False}


def test_it_is_set_once_per_attach_with_the_page_acting_as_focused(sent) -> None:
    methods = [command["method"] for command in sent]
    assert methods.count("Emulation.setDeviceMetricsOverride") == 1  # the second attach sent nothing
    assert "Emulation.setFocusEmulationEnabled" in methods
