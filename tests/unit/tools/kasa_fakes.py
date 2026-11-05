"""A Kasa device as python-kasa 0.10 presents one, for tests that cannot reach a real bulb."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kasa import Module
from kasa.interfaces.light import HSV, LightState


@dataclass
class _Range:
    minimum_value: int
    maximum_value: int


class FakeLight:
    """The Light module: what the bulb can do, and its state."""

    def __init__(
        self, bulb: FakeBulb, *, dim: bool = True, color: bool = True, white: tuple[int, int] | None = (2500, 6500)
    ):
        self._bulb = bulb
        self._features = {name for name, has in (("brightness", dim), ("hsv", color), ("color_temp", white)) if has}
        self._white = _Range(*white) if white else None
        self.brightness = 50
        self.color_temp = white[0] if white else 0
        self.hsv = HSV(0, 0, 50)
        self.sent: list[LightState] = []

    def has_feature(self, name: str) -> bool:
        return name in self._features

    def get_feature(self, name: str) -> Any:
        return self._white if name == "color_temp" else None

    async def set_state(self, state: LightState) -> dict:
        self._bulb.check()
        self.sent.append(state)
        self._bulb.is_on = state.light_on is not False
        if state.brightness is not None:
            self.brightness = state.brightness
        if state.color_temp is not None:
            self.color_temp = state.color_temp
        if state.hue is not None:
            self.hsv = HSV(state.hue, state.saturation or 0, self.brightness)
        return {}


class FakeBulb:
    """A device: a bulb with a Light module, or a plug or switch with none."""

    def __init__(self, host: str, alias: str, *, light: dict[str, Any] | None = None, plug: bool = False) -> None:
        self.host = host
        self.alias = alias
        self.device_id = f"id-{alias.lower().replace(' ', '-')}"
        self.model = "HS103" if plug else "KL130"
        self.is_on = False
        self.modules: dict[Any, Any] = {} if plug else {Module.Light: FakeLight(self, **(light or {}))}
        self.updates = 0
        self.down = False  # the connection dropped: every call fails until north connects again
        self.closed = False

    @property
    def light(self) -> FakeLight:
        return self.modules[Module.Light]

    def check(self) -> None:
        if self.down:
            raise ConnectionError("connection reset by peer")

    async def update(self) -> None:
        self.check()
        self.updates += 1

    async def turn_on(self) -> None:
        self.check()
        self.is_on = True

    async def turn_off(self) -> None:
        self.check()
        self.is_on = False

    async def disconnect(self) -> None:
        self.closed = True


class FakeNetwork:
    """The bulbs discovery finds, and a way to open each one, counting both."""

    def __init__(self, *bulbs: FakeBulb, diagnostic: str = "") -> None:
        self.bulbs = {bulb.host: bulb for bulb in bulbs}
        self.diagnostic = diagnostic
        self.discoveries = 0
        self.opened: list[str] = []
        self.refuse: dict[str, Exception] = {}

    def discover(self) -> tuple[list[tuple[str, str]], str]:
        self.discoveries += 1
        return [(host, bulb.alias) for host, bulb in self.bulbs.items()], self.diagnostic

    async def open(self, host: str) -> FakeBulb:
        self.opened.append(host)
        if host in self.refuse:
            raise self.refuse[host]
        bulb = self.bulbs[host]
        bulb.down = False
        return bulb
