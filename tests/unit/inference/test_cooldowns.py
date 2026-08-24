"""Unit tests for the cooldown store, focused on Retry-After-aware rate limits."""

from __future__ import annotations

import time

from inference.cooldowns import _MAX_RATE_LIMIT_SECS, _RATE_LIMIT_SECS, CooldownStore

_KEY = ("model-x", "provider-y")


def _remaining(store: CooldownStore, key: tuple[str, str]) -> float:
    return store._expiry[key] - time.monotonic()


def test_default_rate_limit_uses_constant() -> None:
    store = CooldownStore()
    store.set_rate_limit(_KEY)
    assert store.is_active(_KEY)
    assert _remaining(store, _KEY) <= _RATE_LIMIT_SECS + 0.5


def test_retry_after_seconds_are_honoured() -> None:
    store = CooldownStore()
    store.set_rate_limit(_KEY, seconds=5)
    remaining = _remaining(store, _KEY)
    assert 4.0 < remaining <= 5.5  # not the default 60


def test_huge_retry_after_is_clamped() -> None:
    store = CooldownStore()
    store.set_rate_limit(_KEY, seconds=10_000)
    assert _remaining(store, _KEY) <= _MAX_RATE_LIMIT_SECS + 0.5


def test_negative_retry_after_is_floored_to_zero() -> None:
    store = CooldownStore()
    store.set_rate_limit(_KEY, seconds=-30)
    # Immediately (or good as) expired - never a negative-duration cooldown.
    assert _remaining(store, _KEY) <= 0.5


def test_unknown_key_is_not_active() -> None:
    assert CooldownStore().is_active(("nope", "nope")) is False


def test_payment_exhausted_persists_to_disk_and_reloads(tmp_path) -> None:
    file_path = tmp_path / "cooldowns.json"
    store1 = CooldownStore(path=file_path)
    store1.set_payment_exhausted(_KEY)
    assert store1.is_active(_KEY) is True
    assert store1.is_payment_required(_KEY[1], _KEY[0]) is True

    # Reload fresh instance from disk
    store2 = CooldownStore(path=file_path)
    store2.load()
    assert store2.is_active(_KEY) is True
    assert store2.is_payment_required(_KEY[1], _KEY[0]) is True
