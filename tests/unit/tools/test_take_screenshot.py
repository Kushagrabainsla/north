"""Unit tests for TakeScreenshotTool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from tools.models import ToolInput
from tools.universal.take_screenshot import TakeScreenshotTool


def _mock_image():
    return Image.new("RGBA", (100, 100), color=(255, 0, 0, 255))


async def test_screenshot_creates_file(tmp_path: Path) -> None:
    """Pure Python ImageGrab creates the file; tool reports success."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot.png"
    with patch("tools.universal.take_screenshot.ImageGrab.grab", return_value=_mock_image()):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert out.data["paths"] == [str(dest)]
    assert out.data["size_bytes"] > 0
    assert out.data["base64_image"] is not None
    assert out.data["mime_type"] == "image/jpeg"
    assert dest.exists()


async def test_screenshot_default_filename(tmp_path: Path) -> None:
    """When no path is given, a timestamped filename is used."""
    tool = TakeScreenshotTool()
    with patch("tools.universal.take_screenshot.ImageGrab.grab", return_value=_mock_image()):
        out = await tool.run(ToolInput(params={"workspace": str(tmp_path)}))

    assert out.success, out.error
    assert "screenshot_" in out.data["path"]
    assert out.data["path"].endswith(".png")


async def test_screenshot_invalid_display(tmp_path: Path) -> None:
    """A non-integer or <1 display value is rejected."""
    tool = TakeScreenshotTool()
    out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path), "display": 0}))
    assert not out.success
    assert "display" in out.error.lower()


async def test_screenshot_blocked_path(tmp_path: Path) -> None:
    """Path that escapes the workspace is rejected."""
    tool = TakeScreenshotTool()
    out = await tool.run(ToolInput(params={"path": "/etc/screenshot.png", "workspace": str(tmp_path)}))
    assert not out.success
    assert "escapes" in out.error.lower() or "blocked" in out.error.lower()


async def test_screenshot_failure_handled(tmp_path: Path) -> None:
    """Exceptions during capture are caught and reported cleanly."""
    tool = TakeScreenshotTool()
    with patch("tools.universal.take_screenshot.ImageGrab.grab", side_effect=RuntimeError("display server unreachable")):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert not out.success
    assert "display server unreachable" in out.error
