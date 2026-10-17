"""Screen brightness: one interface, one implementation per platform.

Callers ask `current()` and never branch on the platform (CODING_STYLE §16.4).
macOS sets the built-in screen through DisplayServices and external monitors
over DDC/CI. A platform with no implementation returns None, and the
brightness tool says this computer is not supported. Add Linux (brightnessctl,
ddcutil) or Windows (WmiSetBrightness) by writing a class with these three
methods (`available`, `screens`, `brightness`) and listing it in
`_implementations()`.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol


class Screen(NamedTuple):
    name: str
    built_in: bool


class Display(Protocol):
    @staticmethod
    def available() -> bool:
        """True when this host can read and set screen brightness."""
        ...

    @staticmethod
    async def screens() -> list[Screen]:
        """The screens whose brightness this host can try, the built-in one first."""
        ...

    @staticmethod
    async def brightness(screen: Screen, level: int | None = None) -> int:
        """Set *screen* to *level* (0 to 100) when given, and return the level it reports.

        Raises OSError, with a reason a person can read, when the screen cannot be read or set."""
        ...


def _implementations() -> tuple[type[Display], ...]:
    # Imported here: the implementations build `Screen`, so they import this module.
    from tools.specialized._mac_display import MacDisplay

    return (MacDisplay,)


def current() -> type[Display] | None:
    """The display control for this host, or None when there is none."""
    return next((impl for impl in _implementations() if impl.available()), None)
