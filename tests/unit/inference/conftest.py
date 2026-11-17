"""Fixtures for the inference tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def no_inline_waits(monkeypatch):
    """The router fails a call at once instead of waiting out its cooldowns.

    For tests about what a failure records, not about waiting: the router now waits
    up to 40 s across several walks, which a test would spend asleep.
    """
    monkeypatch.setattr("inference.routing.router._MAX_INLINE_WAIT_SECONDS", 0.0)
