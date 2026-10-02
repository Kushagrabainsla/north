"""The OS sandbox for shell commands: one interface, one implementation per platform.

Callers ask `current()` and never branch on the platform (CODING_STYLE §16.4).
macOS has Seatbelt. A platform with no implementation returns None, and the
caller falls back to asking for every command - it fails closed, it does not
pretend to be sandboxed. Add Linux (bubblewrap) by writing a class with these
three methods and listing it in `_IMPLEMENTATIONS`.
"""

from __future__ import annotations

from typing import Protocol

from tools.specialized._seatbelt import Seatbelt


class OsSandbox(Protocol):
    @staticmethod
    def available() -> bool:
        """True when this host can confine a command."""
        ...

    @staticmethod
    def wrap(command: str, workspace: str | None, *, writable: bool) -> list[str]:
        """The argv that runs *command* read-only, or writing only inside *workspace*."""
        ...

    @staticmethod
    def denied(stderr: str) -> bool:
        """True when *stderr* shows the sandbox refused something the command tried."""
        ...


_IMPLEMENTATIONS: tuple[type[OsSandbox], ...] = (Seatbelt,)


def current() -> type[OsSandbox] | None:
    """The sandbox for this host, or None when there is none."""
    return next((impl for impl in _IMPLEMENTATIONS if impl.available()), None)
