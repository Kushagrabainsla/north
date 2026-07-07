"""Tests for the unified approval mode dial (interactive / auto / autonomous)."""

from __future__ import annotations

from types import SimpleNamespace

from approval.mode import ApprovalMode, parse_approval_mode, resolve_approval_mode


def _settings(**kw) -> SimpleNamespace:
    base = {"approval_mode": "", "unattended_mode": False, "autonomous_mode": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_parse_canonical_and_synonyms():
    assert parse_approval_mode("interactive") is ApprovalMode.INTERACTIVE
    assert parse_approval_mode("readonly") is ApprovalMode.INTERACTIVE
    assert parse_approval_mode("auto") is ApprovalMode.AUTO
    assert parse_approval_mode("unattended") is ApprovalMode.AUTO
    assert parse_approval_mode("autonomous") is ApprovalMode.AUTONOMOUS
    assert parse_approval_mode("yolo") is ApprovalMode.AUTONOMOUS
    assert parse_approval_mode("  AUTO  ") is ApprovalMode.AUTO


def test_parse_empty_or_unknown_is_none():
    assert parse_approval_mode("") is None
    assert parse_approval_mode(None) is None
    assert parse_approval_mode("bogus") is None


def test_resolve_defaults_to_interactive():
    assert resolve_approval_mode(_settings()) is ApprovalMode.INTERACTIVE


def test_resolve_explicit_mode_wins():
    assert resolve_approval_mode(_settings(approval_mode="autonomous")) is ApprovalMode.AUTONOMOUS


def test_resolve_falls_back_to_legacy_booleans():
    assert resolve_approval_mode(_settings(unattended_mode=True)) is ApprovalMode.AUTO
    assert resolve_approval_mode(_settings(autonomous_mode=True)) is ApprovalMode.AUTONOMOUS


def test_explicit_mode_overrides_legacy_boolean():
    # approval_mode set to interactive must win even if a legacy boolean is on
    s = _settings(approval_mode="interactive", autonomous_mode=True)
    assert resolve_approval_mode(s) is ApprovalMode.INTERACTIVE
