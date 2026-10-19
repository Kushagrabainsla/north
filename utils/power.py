"""Is this computer really awake: one interface, one implementation per platform.

Callers ask `fully_awake()` and never branch on the platform (CODING_STYLE §16.4).
macOS tells a real wake from the short ones it makes while asleep. A platform
with no implementation counts as awake, so work waiting for it still runs. Add
Linux (systemd's sleep state) by writing a class with `available` and
`fully_awake`, and listing it in `_IMPLEMENTATIONS`.
"""

from __future__ import annotations

from typing import Protocol

from utils._mac_power import MacPower


class Power(Protocol):
    @staticmethod
    def available() -> bool:
        """True when this host can say whether it is awake."""
        ...

    @staticmethod
    def fully_awake() -> bool:
        """True when the computer is awake for its user, not in a background wake."""
        ...


_IMPLEMENTATIONS: tuple[type[Power], ...] = (MacPower,)


def fully_awake() -> bool:
    """True unless this host can tell it is asleep and says so."""
    power = next((impl for impl in _IMPLEMENTATIONS if impl.available()), None)
    return True if power is None else power.fully_awake()
