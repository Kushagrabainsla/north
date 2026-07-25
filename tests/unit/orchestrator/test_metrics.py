"""Tests for Incident Metrics & Dashboards Pipeline."""

from __future__ import annotations

from orchestrator.metrics import IncidentMetricsTracker


def test_incident_metrics_tracker():
    tracker = IncidentMetricsTracker()
    tracker.record_task_duration(1.2)
    tracker.record_task_duration(2.5)
    tracker.record_failure("timeout")
    tracker.record_circuit_trip("openencode")
    tracker.record_reconcile_heal(3)

    summary = tracker.get_dashboard_summary()
    assert summary["total_tasks"] == 2
    assert summary["latency_p50_s"] > 0
    assert summary["failure_taxonomy"]["timeout"] == 1
    assert summary["circuit_breaker_trips"]["openencode"] == 1
    assert summary["reconcile_auto_heals"] == 3
