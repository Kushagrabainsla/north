"""Copy a folder without copying its data: one interface, one implementation per platform.

Callers ask `clone_tree()` and never branch on the platform (CODING_STYLE §16.4). On macOS's APFS a
clone shares the source's blocks until one side writes, so a 59,000-file `node_modules` clones in half
a second and takes no space. A platform with no implementation clones nothing, and the caller goes on
without the folder. Add Linux (`ioctl(FICLONE)` on btrfs or XFS) by writing a class with `available`
and `clone`, and listing it in `_IMPLEMENTATIONS`.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class Cloner(Protocol):
    @staticmethod
    def available() -> bool:
        """True when this host can clone a folder."""
        ...

    @staticmethod
    def clone(source: Path, target: Path) -> bool:
        """Clone *source* to *target*, which must not exist; False when it could not."""
        ...


class MacClone:
    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def clone(source: Path, target: Path) -> bool:
        # clonefile(2) clones a whole folder in one call, or fails leaving nothing (not APFS, say).
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(target), 0) == 0:
            return True
        logger.info("could not clone %s: %s", source, os.strerror(ctypes.get_errno()))
        return False


_IMPLEMENTATIONS: tuple[type[Cloner], ...] = (MacClone,)


def clone_tree(source: Path, target: Path) -> bool:
    """Clone the folder *source* to *target*; False when this host cannot."""
    cloner = next((impl for impl in _IMPLEMENTATIONS if impl.available()), None)
    return cloner is not None and cloner.clone(source, target)
