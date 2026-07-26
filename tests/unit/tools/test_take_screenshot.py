"""Unit tests for TakeScreenshotTool."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.models import ToolInput
from tools.universal.take_screenshot import TakeScreenshotTool


async def test_screenshot_creates_file(tmp_path: Path) -> None:
    """Mocked screencapture creates the file; tool reports success."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot.png"

    def _fake_run(cmd, **kwargs):
        # Simulate screencapture writing a file
        dest.write_bytes(b"\x89PNG fake")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert out.data["size_bytes"] > 0
    assert out.data["base64_image"] is not None
    assert out.data["mime_type"] == "image/png"


async def test_screenshot_default_filename(tmp_path: Path) -> None:
    """When no path is given, a timestamped filename is used."""
    tool = TakeScreenshotTool()

    def _fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"\x89PNG")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"workspace": str(tmp_path)}))

    assert out.success, out.error
    assert "screenshot_" in out.data["path"]
    assert out.data["path"].endswith(".png")


async def test_screenshot_blocked_path(tmp_path: Path) -> None:
    """Path that escapes the workspace is rejected."""
    tool = TakeScreenshotTool()
    with patch.object(sys, "platform", "darwin"):
        out = await tool.run(ToolInput(params={"path": "/etc/screenshot.png", "workspace": str(tmp_path)}))
    assert not out.success
    assert "escapes" in out.error.lower() or "blocked" in out.error.lower()


async def test_screenshot_screencapture_fails(tmp_path: Path) -> None:
    """Non-zero exit from screencapture reports failure."""
    tool = TakeScreenshotTool()

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "screen recording permission denied"
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert not out.success
    assert "permission" in out.error.lower()


async def test_screenshot_non_macos() -> None:
    """On non-macOS platforms, the tool returns a clean error."""
    tool = TakeScreenshotTool()
    with patch.object(sys, "platform", "linux"):
        out = await tool.run(ToolInput(params={"path": "shot.png"}))
    assert not out.success
    assert "macOS" in out.error
