"""TakeScreenshotTool - platform-agnostic screen capture tool in pure Python.

Uses Pillow (PIL.ImageGrab) to capture displays across macOS, Linux, and Windows
without external CLI dependencies or OS-specific branching.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from pathlib import Path
from typing import Any

from PIL import ImageGrab
from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class TakeScreenshotTool(Tool):
    """Takes a screenshot of the user's screens in pure Python."""

    name = "take_screenshot"
    is_mutating = True
    description = (
        "Capture what is visible on the user's active screens/monitors. Returns the captured image "
        "directly into your visual context for inspection, reading open apps/windows, and analyzing screen activity."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Destination PNG file path, e.g. 'screenshot.png' or 'docs/screen.png' (optional)."
                ),
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
            "display": {
                "type": "integer",
                "description": "Capture only this 1-based display index (optional, defaults to all screens).",
            },
        },
    }

    def format_output(self, data: dict[str, Any]) -> str:
        paths = data.get("paths", [data.get("path", "?")])
        total = data.get("size_bytes", 0)
        return f"Screenshot saved to {len(paths)} file(s): {', '.join(paths)} ({total} bytes total)."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            path_str = f"screenshot_{int(time.time())}.png"

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root or points to blocked directory.")

        display = input.params.get("display")
        if display is not None:
            try:
                display = int(display)
            except (TypeError, ValueError):
                return ToolOutput(success=False, error="Parameter 'display' must be an integer.")
            if display < 1:
                return ToolOutput(success=False, error="Parameter 'display' must be >= 1.")

        return await asyncio.to_thread(_capture_screen_pure_python, resolved)


def _capture_screen_pure_python(path: Path) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Pure Python screen grab across macOS, Linux, and Windows
        img = ImageGrab.grab(all_screens=True)
        img.save(str(path))
        size_bytes = path.stat().st_size

        # 2. Resize and compress preview in-memory for vision LLM inference
        preview = img.copy()
        preview.thumbnail((1600, 1600))
        if preview.mode in ("RGBA", "P"):
            preview = preview.convert("RGB")

        buf = io.BytesIO()
        preview.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return ToolOutput(
            success=True,
            data={
                "paths": [str(path)],
                "path": str(path),
                "size_bytes": size_bytes,
                "base64_image": b64,
                "mime_type": "image/jpeg",
            },
        )
    except Exception as exc:
        return ToolOutput(success=False, error=f"Screenshot capture failed: {exc}")
