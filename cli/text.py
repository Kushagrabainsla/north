"""`north text FILE`: print the text in a PDF, a Word document or a text file.

Kept apart from `cli.main` on purpose. Shell commands run it inside north's
sandbox (issue #78), where north's home is unreadable, and importing `cli.main`
loads north's settings, which read `.env` in that home and stop with
"Operation not permitted". This module loads nothing of north but the
document readers.
"""

from __future__ import annotations

import sys
from pathlib import Path

USAGE = "usage: north text FILE   (a PDF, a Word document or a text file)"


def print_text(path: Path) -> int:
    """Print the text in *path*. Returns the exit status: 0, or 1 with the reason on stderr."""
    from utils.documents import file_text

    try:
        sys.stdout.write(file_text(path.expanduser()))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


def main(argv: list[str]) -> int:
    """Run `north ARGS` inside a sandboxed command, where only `north text` is offered."""
    if len(argv) == 2 and argv[0] == "text":
        return print_text(Path(argv[1]))
    sys.stderr.write(f"{USAGE}\nOnly `north text` works inside a shell command north runs.\n")
    return 2
