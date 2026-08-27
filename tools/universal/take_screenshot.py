"""TakeScreenshotTool - multi-monitor platform-agnostic screen capture in pure Python.

Uses MSS and Pillow to capture single or all connected monitors across macOS,
Linux, and Windows without external CLI dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from pathlib import Path
from typing import Any

import mss  # noqa: F401  # retained for compatibility with screenshot test integrations
from PIL import Image

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class TakeScreenshotTool(Tool):
    """Takes a screenshot of the user's screens in pure Python."""

    name = "take_screenshot"
    is_mutating = True
    description = (
        "Capture what is visible on the user's active screens/monitors. Supports single or multi-monitor setups. "
        "Returns the captured display directly into your visual context for analyzing screen activity, "
        "open windows, and apps."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Destination image file path, e.g. 'screenshot.png' or 'docs/screen.png' (optional)."
                ),
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
            "display": {
                "type": "integer",
                "description": (
                    "Capture a specific 1-based display index (e.g. 1 = main, 2 = second monitor, 3 = third monitor). "
                    "Pass 0 or omit to capture ALL connected monitors simultaneously in a combined panoramic view."
                ),
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
            if display == 0:
                display = None  # 0 explicitly means all connected monitors
            elif display < 0:
                return ToolOutput(
                    success=False,
                    error="Parameter 'display' must be >= 0 (0 for all displays, 1+ for specific monitor).",
                )

        return await asyncio.to_thread(_capture_screen_pure_python, resolved, display)


def _capture_screen_pure_python(path: Path, display: int | None = None) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        total_bytes = 0
        primary_img: Image.Image | None = None

        # 1. Multi-monitor capture via MSS
        try:
            import mss

            with mss.MSS() as sct:
                monitor_count = len(sct.monitors) - 1  # index 0 is virtual bounding box

                if display is not None:
                    if display > monitor_count:
                        return ToolOutput(
                            success=False,
                            error=f"Display {display} not found. System has {monitor_count} connected display(s).",
                        )
                    sct_img = sct.grab(sct.monitors[display])
                    primary_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    primary_img.save(str(path))
                    written.append(path)
                    total_bytes += path.stat().st_size
                elif monitor_count > 1:
                    # Capture combined desktop as primary visual preview
                    combined_sct = sct.grab(sct.monitors[0])
                    primary_img = Image.frombytes("RGB", combined_sct.size, combined_sct.bgra, "raw", "BGRX")
                    primary_img.save(str(path))
                    written.append(path)
                    total_bytes += path.stat().st_size

                    # Also save individual monitor files
                    stem = path.with_suffix("")
                    suffix = path.suffix or ".png"
                    for idx in range(1, monitor_count + 1):
                        mon_sct = sct.grab(sct.monitors[idx])
                        mon_img = Image.frombytes("RGB", mon_sct.size, mon_sct.bgra, "raw", "BGRX")
                        mon_path = Path(f"{stem}_d{idx}{suffix}")
                        mon_img.save(str(mon_path))
                        written.append(mon_path)
                        total_bytes += mon_path.stat().st_size
                else:
                    sct_img = sct.grab(sct.monitors[1])
                    primary_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    primary_img.save(str(path))
                    written.append(path)
                    total_bytes += path.stat().st_size
        except Exception:
            # Fallback to PIL ImageGrab
            from PIL import ImageGrab

            primary_img = ImageGrab.grab(all_screens=True)
            primary_img.save(str(path))
            written.append(path)
            total_bytes += path.stat().st_size

        if primary_img is None:
            primary_img = Image.open(path)

        # 2. Resize and compress preview in-memory for vision LLM inference
        preview = primary_img.copy()
        preview.thumbnail((1600, 1600))
        if preview.mode in ("RGBA", "P"):
            preview = preview.convert("RGB")

        buf = io.BytesIO()
        preview.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return ToolOutput(
            success=True,
            data={
                "paths": [str(p) for p in written],
                "path": str(written[0]),
                "size_bytes": total_bytes,
                "base64_image": b64,
                "mime_type": "image/jpeg",
            },
        )
    except Exception as exc:
        return ToolOutput(success=False, error=f"Screenshot capture failed: {exc}")
