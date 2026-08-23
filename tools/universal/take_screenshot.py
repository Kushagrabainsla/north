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
                "description": (
                    "Destination PNG file path, e.g. 'screenshot.png' or 'docs/screen.png' (optional). "
                    "When capturing all monitors, this is used as a base name with a per-display suffix "
                    "(e.g. screen_d1.png)."
                ),
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
            "display": {
                "type": "integer",
                "description": (
                    "Capture only this 1-based display index (1 = main). "
                    "Defaults to capturing ALL connected displays, one file per display."
                ),
            },
        },
    }

    def format_output(self, data: dict[str, Any]) -> str:
        paths = data.get("paths", [data.get("path", "?")])
        total = data.get("size_bytes", 0)
        return f"Screenshot saved to {len(paths)} file(s): {', '.join(paths)} ({total} bytes total)."

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

        display = input.params.get("display")
        if display is not None:
            try:
                display = int(display)
            except (TypeError, ValueError):
                return ToolOutput(success=False, error="Parameter 'display' must be an integer.")
            if display < 1:
                return ToolOutput(success=False, error="Parameter 'display' must be >= 1.")

        return await asyncio.to_thread(_capture_sync, resolved, display)


def _count_displays() -> int:
    """Return the number of online displays via system_profiler."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return 1
    # Count displays that are reported as online.
    return max(1, out.count("Online: Yes"))


def _capture_one(path: Path, display: int | None) -> None:
    """Capture a single display (or the main one when display is None)."""
    cmd = ["screencapture", "-x"]
    if display is not None:
        cmd += ["-D", str(display)]
    cmd.append(str(path))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        raise RuntimeError(f"screencapture failed: {res.stderr}")


def _capture_sync(path: Path, display: int | None) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        if display is not None:
            # Single display requested.
            targets = [(display, path)]
        else:
            # Default: all connected displays, one file per display.
            count = _count_displays()
            if count <= 1:
                targets = [(None, path)]
            else:
                # Insert a per-display suffix before the extension.
                stem = path.with_suffix("")
                suffix = path.suffix or ".png"
                targets = [(i, Path(f"{stem}_d{i}{suffix}")) for i in range(1, count + 1)]

        written: list[Path] = []
        total_bytes = 0
        for disp, out_path in targets:
            _capture_one(out_path, disp)
            if not out_path.exists():
                return ToolOutput(success=False, error=f"Screenshot file was not created: {out_path}")
            image_bytes = out_path.read_bytes()
            total_bytes += len(image_bytes)
            written.append(out_path)

        # Encode only the first image into base64 (the rest are on disk).
        first = written[0].read_bytes()
        b64 = base64.b64encode(first).decode("ascii")
        mime, _ = mimetypes.guess_type(str(written[0]))
        if mime is None:
            mime = "image/png"

        return ToolOutput(
            success=True,
            data={
                "paths": [str(p) for p in written],
                "path": str(written[0]),
                "size_bytes": total_bytes,
                "base64_image": b64,
                "mime_type": mime,
            },
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error="screencapture timed out after 10s.")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
