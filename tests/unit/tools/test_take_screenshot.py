"""Unit tests for TakeScreenshotTool."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.models import ToolInput
from tools.universal.take_screenshot import TakeScreenshotTool

_FAKE_PNG = b"\x89PNG fake"


def _make_result(returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    return result


def _fake_run_all_displays(cmd, **kwargs):
    """Handle both the system_profiler (count) and screencapture calls.

    system_profiler SPDisplaysDataType -> report 1 online display.
    screencapture -x [-D N] <path> -> write a fake PNG at the last arg.
    """
    if cmd[:2] == ["system_profiler", "SPDisplaysDataType"]:
        result = _make_result()
        result.stdout = "Online: Yes\n"
        return result
    # screencapture: last arg is the destination path.
    Path(cmd[-1]).write_bytes(_FAKE_PNG)
    return _make_result()


async def test_screenshot_creates_file(tmp_path: Path) -> None:
    """Mocked screencapture creates the file; tool reports success."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot.png"
    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run_all_displays),
    ):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert out.data["paths"] == [str(dest)]
    assert out.data["size_bytes"] > 0
    assert out.data["base64_image"] is not None
    assert out.data["mime_type"] in ("image/jpeg", "image/png")


async def test_screenshot_default_filename(tmp_path: Path) -> None:
    """When no path is given, a timestamped filename is used."""
    tool = TakeScreenshotTool()
    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run_all_displays),
    ):
        out = await tool.run(ToolInput(params={"workspace": str(tmp_path)}))

    assert out.success, out.error
    assert "screenshot_" in out.data["path"]
    assert out.data["path"].endswith(".png")


async def test_screenshot_captures_all_displays(tmp_path: Path) -> None:
    """Default behaviour captures every online display (one file each)."""
    tool = TakeScreenshotTool()
    base = tmp_path / "all.png"

    def _fake_run_multi(cmd, **kwargs):
        if cmd[:2] == ["system_profiler", "SPDisplaysDataType"]:
            result = _make_result()
            # Three online displays.
            result.stdout = "Online: Yes\nOnline: Yes\nOnline: Yes\n"
            return result
        Path(cmd[-1]).write_bytes(_FAKE_PNG)
        return _make_result()

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run_multi),
    ):
        out = await tool.run(ToolInput(params={"path": "all.png", "workspace": str(tmp_path)}))

    assert out.success, out.error
    paths = out.data["paths"]
    assert len(paths) == 3
    # Each display gets a distinct per-display suffix.
    assert str(base.with_name("all_d1.png")) in paths
    assert str(base.with_name("all_d2.png")) in paths
    assert str(base.with_name("all_d3.png")) in paths


async def test_screenshot_single_display(tmp_path: Path) -> None:
    """When display=N is given, only that one monitor is captured."""
    tool = TakeScreenshotTool()
    dest = tmp_path / "shot.png"
    captured = []

    def _fake_run_single(cmd, **kwargs):
        if cmd[:2] == ["system_profiler", "SPDisplaysDataType"]:
            result = _make_result()
            result.stdout = "Online: Yes\n"
            return result
        captured.append(cmd)
        Path(cmd[-1]).write_bytes(_FAKE_PNG)
        return _make_result()

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_screenshot.subprocess.run", side_effect=_fake_run_single),
    ):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path), "display": 2}))

    assert out.success, out.error
    assert out.data["paths"] == [str(dest)]
    # screencapture must have been passed -D 2.
    assert captured and "-D" in captured[0] and "2" in captured[0]


async def test_screenshot_invalid_display(tmp_path: Path) -> None:
    """A non-integer or <1 display value is rejected."""
    tool = TakeScreenshotTool()
    with patch.object(sys, "platform", "darwin"):
        out = await tool.run(ToolInput(params={"path": "shot.png", "workspace": str(tmp_path), "display": 0}))
    assert not out.success
    assert "display" in out.error.lower()


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
        if cmd[:2] == ["system_profiler", "SPDisplaysDataType"]:
            result = _make_result()
            result.stdout = "Online: Yes\n"
            return result
        return _make_result(returncode=1, stderr="screen recording permission denied")

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
