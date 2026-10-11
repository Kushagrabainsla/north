"""Run the browser-window ownership contract without Chrome or npm installs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_task_window_ownership_and_recovery_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; existing-profile browsers require it")
    script = Path(__file__).with_name("browser_windows.test.mjs")
    output = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=30)
    assert output.returncode == 0, output.stdout + output.stderr
