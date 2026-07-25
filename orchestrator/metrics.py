"""Incident Dashboards & Operational Metrics Pipeline (Deliverable 8).

Aggregates runtime telemetry for latency (p50/p95/p99), provider circuit breaker states,
failure taxonomy breakdowns, and state reconciliation auto-heal statistics.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncidentMetricsTracker:
    """In-memory operational metrics collector and dashboard exporter."""

    _latencies_s: list[float] = field(default_factory=list)
    _failure_counts: dict[str, int] = field(default_factory=dict)
    _circuit_trips: dict[str, int] = field(default_factory=dict)
    _reconcile_heals: int = 0
    _total_tasks: int = 0

    def record_task_duration(self, duration_s: float) -> None:
        """Record task execution latency in seconds."""
        self._total_tasks += 1
        self._latencies_s.append(max(0.0, duration_s))
        if len(self._latencies_s) > 10_000:
            self._latencies_s = self._latencies_s[-5000:]

    def record_failure(self, category: str) -> None:
        """Record a failure categorized by error taxonomy (auth, billing, quota, timeout, etc.)."""
        cat = category.strip().lower() or "unknown"
        self._failure_counts[cat] = self._failure_counts.get(cat, 0) + 1

    def record_circuit_trip(self, provider_name: str) -> None:
        """Record a provider circuit breaker trip event."""
        self._circuit_trips[provider_name] = self._circuit_trips.get(provider_name, 0) + 1

    def record_reconcile_heal(self, count: int = 1) -> None:
        """Record auto-healed task state drift count."""
        self._reconcile_heals += max(0, count)

    def get_dashboard_summary(self) -> dict[str, Any]:
        """Export comprehensive incident metrics dashboard payload."""
        latencies = sorted(self._latencies_s)
        n = len(latencies)
        p50 = statistics.median(latencies) if n > 0 else 0.0
        p95 = latencies[int(n * 0.95)] if n > 0 else 0.0
        p99 = latencies[int(n * 0.99)] if n > 0 else 0.0

        return {
            "total_tasks": self._total_tasks,
            "latency_p50_s": round(p50, 3),
            "latency_p95_s": round(p95, 3),
            "latency_p99_s": round(p99, 3),
            "failure_taxonomy": dict(self._failure_counts),
            "circuit_breaker_trips": dict(self._circuit_trips),
            "reconcile_auto_heals": self._reconcile_heals,
            "timestamp": time.time(),
        }


# Global singleton instance for app-wide metrics collection
metrics_tracker = IncidentMetricsTracker()
