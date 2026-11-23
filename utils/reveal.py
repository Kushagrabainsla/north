"""Show a file in this computer's file manager: Finder, Explorer, or the Linux file manager.

North shows a file; it never opens one (CODING_STYLE §5.1). A file an agent or the web made can
run as a program when opened, so north selects it in the file manager and you decide what to do.

`click.launch(..., locate=True)` does this on each platform, so no caller branches on the OS
(CODING_STYLE §16.4): `open -R` on macOS, `explorer /select,` on Windows, and `xdg-open` on the
folder on Linux, which opens the folder without selecting the file. A computer with no file
manager north can start, such as north in a container, returns False and nothing is shown.
"""

from __future__ import annotations

from pathlib import Path

import click


def reveal(path: Path) -> bool:
    """Show *path* in the file manager. True when the file manager was started."""
    try:
        return click.launch(str(path), locate=True) == 0
    except OSError:
        return False
