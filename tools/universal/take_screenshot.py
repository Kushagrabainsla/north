"""TakeScreenshotTool - take a screenshot of the user's screen across macOS, Linux, and Windows.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import base64
import io
import mimetypes
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class TakeScreenshotTool(Tool):
    """Takes a screenshot of the user's screens and saves it as an image."""

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
    """Return the number of online displays."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            return max(1, out.count("Online: Yes"))
        except Exception:
            return 1
    return 1


def _capture_one(path: Path, display: int | None) -> None:
    """Capture a single display cross-platform (macOS, Linux, Windows)."""
    # 1. macOS native capture
    if sys.platform == "darwin":
        cmd = ["screencapture", "-x"]
        if display is not None:
            cmd += ["-D", str(display)]
        cmd.append(str(path))
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            raise RuntimeError(f"screencapture failed: {res.stderr}")
        if path.exists():
            return

    # 2. Linux native utilities (Wayland & X11)
    if sys.platform.startswith("linux"):
        for tool_name, args in [
            ("grim", [str(path)]),
            ("scrot", [str(path)]),
            ("gnome-screenshot", ["-f", str(path)]),
            ("import", ["-window", "root", str(path)]),
        ]:
            if shutil.which(tool_name):
                res = subprocess.run([tool_name, *args], capture_output=True, text=True, timeout=10)
                if res.returncode == 0 and path.exists():
                    return

    # 3. Universal Python ImageGrab fallback (macOS, Windows, Linux with X11)
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(all_screens=True if display is None else False)
        img.save(str(path))
        if path.exists():
            return
    except Exception:
        pass

    # 4. Windows PowerShell fallback
    if sys.platform == "win32":
        ps_cmd = (
            f"Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            f"$b = New-Object System.Drawing.Bitmap ([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width), ([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); "
            f"$g = [System.Drawing.Graphics]::FromImage($b); "
            f"$g.CopyFromScreen((New-Object System.Drawing.Point(0,0)), (New-Object System.Drawing.Point(0,0)), $b.Size); "
            f"$b.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)"
        )
        res = subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and path.exists():
            return

    if not path.exists():
        raise RuntimeError("No suitable screenshot capture utility or library found on this system.")


def _compress_for_vision(src_path: Path) -> tuple[str, str]:
    """Compress and resize screenshot for vision LLM inference cross-platform.

    Uses pure-Python PIL (or platform CLI tools as fallback) to reduce full-res
    screens (6MB - 15MB) to crisp 1600px JPEGs (~200KB), preventing context overflows
    while preserving complete legibility.
    """
    # 1. Pure Python PIL (Platform Agnostic)
    try:
        from PIL import Image

        with Image.open(src_path) as img:
            img.thumbnail((1600, 1600))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return b64, "image/jpeg"
    except Exception:
        pass

    # 2. macOS native sips fallback
    if sys.platform == "darwin":
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            res = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", "1600", str(src_path), "--out", str(tmp_path)],
                capture_output=True,
                timeout=5,
            )
            if res.returncode == 0 and tmp_path.exists():
                data = tmp_path.read_bytes()
                tmp_path.unlink(missing_ok=True)
                b64 = base64.b64encode(data).decode("ascii")
                return b64, "image/jpeg"
        except Exception:
            pass

    # 3. Raw file fallback
    raw = src_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    mime, _ = mimetypes.guess_type(str(src_path))
    return b64, mime or "image/png"


def _capture_sync(path: Path, display: int | None) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        if display is not None:
            targets = [(display, path)]
        else:
            count = _count_displays()
            if count <= 1:
                targets = [(None, path)]
            else:
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

        b64, mime = _compress_for_vision(written[0])

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
        return ToolOutput(success=False, error="Screenshot capture timed out after 10s.")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
