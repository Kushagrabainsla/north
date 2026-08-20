"""TakePhotoTool - capture a photo from the user's webcam.

Uses a small Swift helper compiled on-the-fly via AVFoundation to grab
a single frame from the default video capture device. macOS only.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

# Minimal Swift program that captures one frame from the default camera.
# Compiled once into /tmp and reused for subsequent calls within the same
# server lifetime.  AVFoundation requires a brief run-loop spin to warm
# the camera; 1.5 s is generous for a built-in FaceTime camera.
_SWIFT_SOURCE = """import AVFoundation
import AppKit
import CoreMedia
import CoreVideo

// Capture a single still frame using AVCaptureVideoDataOutput (sample-buffer
// delegate) instead of AVCapturePhotoOutput. The photo-output path internally
// builds a KVO proxy class (NSKVONotifying_AVCapturePhotoOutput) that is not
// linked when this binary runs as a detached subprocess, which fails with
// "class NSKVONotifying_AVCapturePhotoOutput not linked into application".
// The video-data path avoids that KVO subclass entirely and is reliable for a
// CLI grab.
class Grabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let sema = DispatchSemaphore(value: 0)
    let outPath: String
    var errMsg: String?
    var gotFrame = false

    init(path: String) { self.outPath = path }

    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        if gotFrame { return }
        gotFrame = true
        autoreleasepool {
            guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
                errMsg = "no image buffer"; sema.signal(); return
            }
            let ciImage = CIImage(cvImageBuffer: imageBuffer)
            let rep = NSBitmapImageRep(ciImage: ciImage)
            guard let data = rep.representation(using: .jpeg, properties: [:]) else {
                errMsg = "encode failed"; sema.signal(); return
            }
            do { try data.write(to: URL(fileURLWithPath: outPath)) }
            catch { errMsg = "write failed: \\(error)" }
        }
        sema.signal()
    }
}

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: capture <output.jpg>\\n", stderr); exit(1)
}
let outPath = CommandLine.arguments[1]

guard let device = AVCaptureDevice.default(for: .video) else {
    fputs("ERROR: No camera found\\n", stderr); exit(2)
}
let session = AVCaptureSession()
session.sessionPreset = .photo
guard let input = try? AVCaptureDeviceInput(device: device) else {
    fputs("ERROR: Cannot open camera input\\n", stderr); exit(3)
}
session.addInput(input)

let output = AVCaptureVideoDataOutput()
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
let grabber = Grabber(path: outPath)
let queue = DispatchQueue(label: "capture.queue")
output.setSampleBufferDelegate(grabber, queue: queue)
guard session.canAddOutput(output) else {
    fputs("ERROR: Cannot add output\\n", stderr); exit(3)
}
session.addOutput(output)
session.startRunning()

// Spin the run loop briefly so the first frame arrives.
let start = Date()
while !grabber.gotFrame && Date().timeIntervalSince(start) < 8.0 {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
}
session.stopRunning()

if let err = grabber.errMsg { fputs("ERROR: \\(err)\\n", stderr); exit(5) }
if !grabber.gotFrame { fputs("ERROR: Capture timed out\\n", stderr); exit(4) }
"""

_COMPILED_BINARY: Path | None = None


class TakePhotoTool(Tool):
    """Captures a photo from the user's webcam and saves it in the workspace."""

    name = "take_photo"
    is_mutating = True
    description = (
        "Capture a photo from the user's webcam (built-in camera) and save it as an image "
        "file in the workspace. Use it to see what is physically in front of the user's "
        "computer, identify objects, read handwritten notes, etc."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination image file path, e.g. 'photo.jpg' (optional)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Photo saved to `{data.get('path', '?')}` ({data.get('size_bytes', 0)} bytes)."

    async def run(self, input: ToolInput) -> ToolOutput:
        if sys.platform != "darwin":
            return ToolOutput(
                success=False,
                error=f"Camera capture is only supported on macOS, but current OS is {sys.platform}.",
            )

        path_str = input.params.get("path")
        if not path_str:
            path_str = f"photo_{int(time.time())}.jpg"

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root or points to blocked directory.")

        return await asyncio.to_thread(_photo_sync, resolved)


def _ensure_binary() -> Path:
    """Compile the Swift helper once; reuse the binary on subsequent calls."""
    global _COMPILED_BINARY
    if _COMPILED_BINARY is not None and _COMPILED_BINARY.exists():
        return _COMPILED_BINARY

    build_dir = Path(tempfile.gettempdir()) / "north_camera"
    build_dir.mkdir(exist_ok=True)
    src = build_dir / "capture.swift"
    binary = build_dir / "capture"
    src.write_text(_SWIFT_SOURCE, encoding="utf-8")

    res = subprocess.run(
        [
            "swiftc",
            "-O",
            "-framework",
            "AVFoundation",
            "-framework",
            "AppKit",
            "-o",
            str(binary),
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Swift compilation failed: {res.stderr}")

    _COMPILED_BINARY = binary
    return binary


def _photo_sync(path: Path) -> ToolOutput:
    try:
        binary = _ensure_binary()
    except Exception as exc:
        return ToolOutput(success=False, error=f"Failed to compile camera helper: {exc}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [str(binary), str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            return ToolOutput(success=False, error=f"Camera capture failed: {res.stderr.strip()}")

        if not path.exists():
            return ToolOutput(success=False, error="Photo file was not created.")

        image_bytes = path.read_bytes()
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime, _ = mimetypes.guess_type(str(path))
        if mime is None:
            mime = "image/jpeg"

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
        return ToolOutput(success=False, error="Camera capture timed out after 15s.")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
