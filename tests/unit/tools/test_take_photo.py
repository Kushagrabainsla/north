"""Unit tests for TakePhotoTool."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.models import ToolInput
from tools.universal.take_photo import TakePhotoTool


async def test_photo_creates_file(tmp_path: Path) -> None:
    """When the Swift helper succeeds, the tool reports the saved photo."""
    tool = TakePhotoTool()
    dest = tmp_path / "photo.jpg"

    def _fake_run(cmd, **kwargs):
        # Simulate the compiled binary writing a JPEG
        dest.write_bytes(b"\xff\xd8\xff fake jpeg")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_photo._ensure_binary", return_value=Path("/tmp/north_camera/capture")),
        patch("tools.universal.take_photo.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"path": "photo.jpg", "workspace": str(tmp_path)}))

    assert out.success, out.error
    assert out.data["path"] == str(dest)
    assert out.data["size_bytes"] > 0
    assert out.data["base64_image"] is not None
    assert out.data["mime_type"] == "image/jpeg"


async def test_photo_default_filename(tmp_path: Path) -> None:
    """When no path is given, a timestamped filename is used."""
    tool = TakePhotoTool()

    def _fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"\xff\xd8\xff fake")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_photo._ensure_binary", return_value=Path("/tmp/north_camera/capture")),
        patch("tools.universal.take_photo.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"workspace": str(tmp_path)}))

    assert out.success, out.error
    assert "photo_" in out.data["path"]


async def test_photo_blocked_path(tmp_path: Path) -> None:
    """Path that escapes the workspace is rejected."""
    tool = TakePhotoTool()
    with patch.object(sys, "platform", "darwin"):
        out = await tool.run(ToolInput(params={"path": "/etc/photo.jpg", "workspace": str(tmp_path)}))
    assert not out.success
    assert "escapes" in out.error.lower() or "blocked" in out.error.lower()


async def test_photo_camera_fails(tmp_path: Path) -> None:
    """Non-zero exit from the camera binary reports failure."""
    tool = TakePhotoTool()

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 2
        result.stderr = "ERROR: No camera found"
        return result

    with (
        patch.object(sys, "platform", "darwin"),
        patch("tools.universal.take_photo._ensure_binary", return_value=Path("/tmp/north_camera/capture")),
        patch("tools.universal.take_photo.subprocess.run", side_effect=_fake_run),
    ):
        out = await tool.run(ToolInput(params={"path": "photo.jpg", "workspace": str(tmp_path)}))

    assert not out.success
    assert "No camera" in out.error


async def test_off_macos_it_says_there_is_no_camera_instead_of_trying() -> None:
    """Issue #76, W11: on Linux agents offered "I can take a photo with your camera", which cannot work there."""
    from tools.universal.take_photo import camera_unavailable

    with patch.object(sys, "platform", "linux"):
        out = await TakePhotoTool().execute(ToolInput(params={"path": "photo.jpg"}))
        assert camera_unavailable()

    assert not out.success and "works only on macOS" in out.error
