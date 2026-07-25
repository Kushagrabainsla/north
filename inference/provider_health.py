"""Provider-level circuit breaker state for multi-provider inference.

The dispatcher already has model-level cooldowns. This tracker adds a provider
layer so repeated auth/billing or bursty generic failures can stop thrashing an
entire provider and let healthier providers take over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_DEFAULT_DEGRADED_SECS = 120.0
_MAX_DEGRADED_SECS = 300.0
_DOWN_SECS = 86_400.0


@dataclass
class ProviderHealthRecord:
    state: str = "healthy"  # healthy | degraded | down
    unhealthy_until: float = 0.0
    consecutive_failures: int = 0
    last_reason: str = ""


class ProviderHealthTracker:
    """Track per-provider health state with a simple circuit breaker."""

    def __init__(
        self,
        *,
        degraded_threshold: int = 2,
        degraded_seconds: float = _DEFAULT_DEGRADED_SECS,
        max_degraded_seconds: float = _MAX_DEGRADED_SECS,
        down_seconds: float = _DOWN_SECS,
    ) -> None:
        self._records: dict[str, ProviderHealthRecord] = {}
        self._degraded_threshold = max(1, degraded_threshold)
        self._degraded_seconds = degraded_seconds
        self._max_degraded_seconds = max_degraded_seconds
        self._down_seconds = down_seconds

    def _record(self, provider_name: str) -> ProviderHealthRecord:
        return self._records.setdefault(provider_name, ProviderHealthRecord())

    def _expire_if_needed(self, provider_name: str) -> ProviderHealthRecord | None:
        record = self._records.get(provider_name)
        if record is None:
            return None
        if record.unhealthy_until > 0 and time.monotonic() >= record.unhealthy_until:
            self._records.pop(provider_name, None)
            return None
        return record

    def is_available(self, provider_name: str) -> bool:
        record = self._expire_if_needed(provider_name)
        if record is None:
            return True
        return record.state == "healthy"

    def record_success(self, provider_name: str) -> None:
        self._records.pop(provider_name, None)

    def mark_degraded(self, provider_name: str, reason: str = "") -> str:
        """Apply a short circuit-breaker window after repeated transient failures."""
        record = self._record(provider_name)
        if record.state == "down" and record.unhealthy_until > time.monotonic():
            return record.state

        record.consecutive_failures += 1
        record.last_reason = reason
        if record.consecutive_failures < self._degraded_threshold:
            return "healthy"

        record.state = "degraded"
        window = min(
            self._max_degraded_seconds,
            self._degraded_seconds * (2 ** (record.consecutive_failures - self._degraded_threshold)),
        )
        record.unhealthy_until = time.monotonic() + window
        return record.state

    def mark_down(self, provider_name: str, reason: str = "") -> str:
        """Open the provider circuit for a long window after hard auth/billing failures."""
        record = self._record(provider_name)
        record.state = "down"
        record.consecutive_failures = 0
        record.last_reason = reason
        record.unhealthy_until = time.monotonic() + self._down_seconds
        return record.state
