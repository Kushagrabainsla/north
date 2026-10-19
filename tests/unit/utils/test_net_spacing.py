"""arXiv is asked at most once every three seconds; other hosts never wait."""

from __future__ import annotations

import pytest

import utils.net as net


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(net.time, "sleep", sleep)
    monkeypatch.setattr(net, "_last_request_at", {})
    return slept


def test_a_second_arxiv_request_waits_out_the_three_seconds(clock: list[float]) -> None:
    """Faster, arXiv answers 429 "Rate exceeded", and the briefing's paper check lost its answer."""
    net._wait_for_turn("https://export.arxiv.org/api/query?search_query=all:%22a%22")
    net._wait_for_turn("https://export.arxiv.org/api/query?id_list=2610.22041")

    assert clock == [3.0]


def test_other_hosts_are_never_held_back(clock: list[float]) -> None:
    for _ in range(3):
        net._wait_for_turn("https://www.python.org/downloads/")

    assert clock == []
