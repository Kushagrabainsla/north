"""TakeScreenshotTool - take a screenshot of the user's screen.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class TakeScreenshotTool(Tool):
    """Takes a screenshot of the user's screens and saves it as a PNG."""

    name = "take_screenshot"
    is_mutating = True
    description = (
        "Take a screenshot of the user's screen and save it as a PNG file in the workspace. "
        "Use it to capture visual output, view browser/app states, or document the desktop."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination PNG file path, e.g. 'screenshot.png' or 'docs/screen.png' (optional)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Screenshot saved to `{data.get('path', '?')}` ({data.get('size_bytes', 0)} bytes)."

    async def run(self, input: ToolInput) -> ToolOutput:
        if sys.platform != "darwin":
            return ToolOutput(
                success=False,
                error=f"Screenshot capability is only supported on macOS, but current OS is {sys.platform}.",
            )

        path_str = input.params.get("path")
        if not path_str:
            path_str = f"screenshot_{int(time.time())}.png"

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root or points to blocked directory.")

        return await asyncio.to_thread(_capture_sync, resolved)


def _capture_sync(path: Path) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # -x: silent mode (no capture sound)
        res = subprocess.run(
            ["screencapture", "-x", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return ToolOutput(success=False, error=f"screencapture failed: {res.stderr}")

        if not path.exists():
            return ToolOutput(success=False, error="Screenshot file was not created.")

        image_bytes = path.read_bytes()
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime, _ = mimetypes.guess_type(str(path))
        if mime is None:
            mime = "image/png"

        return ToolOutput(
            success=True,
            data={
                "path": str(path),
                "size_bytes": len(image_bytes),
                "base64_image": b64,
                "mime_type": mime,
            },
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error="screencapture timed out after 10s.")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
