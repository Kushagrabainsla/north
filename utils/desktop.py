"""The person's desktop: which app is in front, and putting it back there.

North works on the same machine the person is using. When a background action
makes another app jump forward anyway, this hands the front back to the app the
person was in. macOS for now; elsewhere both calls do nothing.
"""

from __future__ import annotations

import subprocess
import sys


def frontmost_app() -> str | None:
    """Bundle ID of the app in front, or None when it cannot be told."""
    if sys.platform != "darwin":
        return None
    try:
        asn = subprocess.run(
            ["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=2, check=True
        ).stdout.strip()
        info = subprocess.run(
            ["/usr/bin/lsappinfo", "info", "-only", "bundleid", asn],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    _, _, value = info.partition("=")
    return value.strip().strip('"') or None


def activate(bundle_id: str) -> None:
    """Bring an already running app back to the front."""
    if sys.platform != "darwin":
        return
    subprocess.run(["/usr/bin/open", "-b", bundle_id], capture_output=True, timeout=5, check=False)
