"""A Mac in one of its short wakes while asleep is not awake for its user."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import utils._mac_power as mac_power
import utils.power as power


@pytest.mark.parametrize(
    ("state", "awake"),
    [
        ("Current System Capabilities are: CPU Graphics Audio Network \nCurrent Power State: 4\n", True),
        ("Current System Capabilities are: CPU Network \nCurrent Power State: 4\n", False),  # a DarkWake
    ],
)
def test_a_real_wake_has_graphics(monkeypatch: pytest.MonkeyPatch, state: str, awake: bool) -> None:
    monkeypatch.setattr(mac_power.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=state))

    assert mac_power.MacPower.fully_awake() is awake


def test_when_pmset_cannot_answer_it_counts_as_awake(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired("pmset", 10)

    monkeypatch.setattr(mac_power.subprocess, "run", broken)

    assert mac_power.MacPower.fully_awake() is True


def test_a_platform_that_cannot_tell_counts_as_awake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_IMPLEMENTATIONS", ())

    assert power.fully_awake() is True
