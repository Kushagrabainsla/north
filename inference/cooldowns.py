"""Cooldown store — tracks rate-limit and payment-exhausted cooldowns per model."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# (model_id, provider_name)
_CooldownKey = tuple[str, str]

_RATE_LIMIT_SECS: float = 60.0
_PAYMENT_EXHAUSTED_SECS: float = 86_400.0  # 24 h


class CooldownStore:
    """Tracks per-model cooldowns in memory with optional disk persistence for payment cooldowns.

    Rate-limit cooldowns (60 s) are memory-only — they reset on restart.
    Payment-exhausted cooldowns (24 h) are persisted to disk so they survive restarts.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._expiry: dict[_CooldownKey, float] = {}  # monotonic timestamps

    def load(self) -> None:
        """Load persisted payment cooldowns from disk, converting wall-clock → monotonic."""
        if self._path is None or not self._path.exists():
            return
        try:
            data: dict[str, float] = json.loads(self._path.read_text())
            now_wall = time.time()
            now_mono = time.monotonic()
            for raw_key, wall_expiry in data.items():
                remaining = wall_expiry - now_wall
                if remaining <= 0:
                    continue
                model_id, _, provider_name = raw_key.partition("::")
                self._expiry[(model_id, provider_name)] = now_mono + remaining
            if self._expiry:
                logger.info("Loaded %d persisted payment cooldown(s) from disk", len(self._expiry))
        except Exception:
            logger.warning("Failed to load cooldowns file — starting fresh", exc_info=True)

    def is_active(self, key: _CooldownKey) -> bool:
        """Return True if the model is currently under cooldown."""
        return self._expiry.get(key, 0.0) > time.monotonic()

    def set_rate_limit(self, key: _CooldownKey) -> None:
        """Apply a short rate-limit cooldown (60 s, memory-only)."""
        self._expiry[key] = time.monotonic() + _RATE_LIMIT_SECS

    def set_payment_exhausted(self, key: _CooldownKey) -> None:
        """Apply a 24-hour payment cooldown and persist it to disk."""
        mono_expiry = time.monotonic() + _PAYMENT_EXHAUSTED_SECS
        self._expiry[key] = mono_expiry
        self._persist(key, mono_expiry)

    def _persist(self, key: _CooldownKey, mono_expiry: float) -> None:
        if self._path is None:
            return
        try:
            wall_expiry = time.time() + max(0.0, mono_expiry - time.monotonic())
            data: dict[str, float] = {}
            if self._path.exists():
                with contextlib.suppress(Exception):
                    data = json.loads(self._path.read_text())
            model_id, provider_name = key
            data[f"{model_id}::{provider_name}"] = wall_expiry
            self._path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("Failed to persist payment cooldown for %s/%s", key[1], key[0], exc_info=True)
