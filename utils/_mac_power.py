"""Whether a Mac is really awake, or only in one of the short wakes it makes while asleep.

A closed MacBook wakes for a few seconds at a time to fetch mail and sync (a
"DarkWake"): the CPU, disk and network come on, the screen does not. Its sleep
log shows the difference, `[CDNP]` for those wakes and `[CDNVA]` for a real one,
V and A being graphics and audio; `pmset -g systemstate` names the same
capabilities for the moment it is asked.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


class MacPower:
    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and shutil.which("pmset") is not None

    @staticmethod
    def fully_awake() -> bool:
        try:
            state = subprocess.run(
                ["pmset", "-g", "systemstate"], capture_output=True, text=True, timeout=10, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return True  # cannot tell: the caller runs rather than wait forever
        capabilities = next((line for line in state.splitlines() if "Capabilities" in line), "")
        return "Graphics" in capabilities
