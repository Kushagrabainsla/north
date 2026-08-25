"""Unit tests for TakeScreenshotTool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

from tools.models import ToolInput
from tools.universal.take_screenshot import TakeScreenshotTool


def _mock_image():
    return Image.new("RGBA", (100, 100), color=(255, 0, 0, 255))


class MockMSS:
    def __init__(self):
        self.monitors = [
            {"left": 0, "top": 0, "width": 300, "height": 100},  # all
            {"left": 0, "top": 0, "width": 100, "height": 100},  # mon 1
            {"left": 100, "top": 0, "width": 100, "height": 100},  # mon 2
            {"left": 200, "top": 0, "width": 100, "height": 100},  # mon 3
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def grab(self, monitor):
        mock_shot = MagicMock()
        mock_shot.size = (monitor["width"], monitor["height"])
        # Mock BGRA bytes: width * height * 4
        mock_shot.bgra = b"\x00\x00\xff\xff" * (monitor["width"] * monitor["height"])
        return mock_shot


async def test_screenshot_creates_file(tmp_path: Path) -> None:
    """Pure Python MSS/ImageGrab creates the file; tool reports success."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot.png"
    with patch("tools.universal.take_screenshot.mss.MSS", side_effect=MockMSS):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert len(out.data["paths"]) == 4  # combined + 3 monitors
    assert out.data["size_bytes"] > 0
    assert out.data["base64_image"] is not None
    assert out.data["mime_type"] == "image/jpeg"
    assert dest.exists()


async def test_screenshot_single_display_selection(tmp_path: Path) -> None:
    """Selecting a specific display index captures only that monitor."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot_d2.png"
    with patch("tools.universal.take_screenshot.mss.MSS", side_effect=MockMSS):
        out = await tool.run(ToolInput(params={"path": "shot_d2.png", "workspace": str(tmp_path), "display": 2}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert len(out.data["paths"]) == 1
    assert dest.exists()


async def test_screenshot_display_out_of_range(tmp_path: Path) -> None:
    """Display index exceeding connected displays returns a helpful error."""
    tool = TakeScreenshotTool()
    with patch("tools.universal.take_screenshot.mss.MSS", side_effect=MockMSS):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path), "display": 5}))

    assert not out.success
    assert "Display 5 not found" in out.error


async def test_screenshot_default_filename(tmp_path: Path) -> None:
    """When no path is given, a timestamped filename is used."""
    tool = TakeScreenshotTool()
    with patch("tools.universal.take_screenshot.mss.MSS", side_effect=MockMSS):
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
