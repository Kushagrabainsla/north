"""Tests for the approval mode dial (ask / safe / autonomous / yolo)."""

from __future__ import annotations

from types import SimpleNamespace

from config.approval_mode import ApprovalMode, parse_approval_mode, resolve_approval_mode


def test_parse_each_mode_whatever_its_case():
    assert parse_approval_mode("ask") is ApprovalMode.ASK
    assert parse_approval_mode("safe") is ApprovalMode.SAFE
    assert parse_approval_mode("autonomous") is ApprovalMode.AUTONOMOUS
    assert parse_approval_mode("yolo") is ApprovalMode.YOLO
    assert parse_approval_mode("  SAFE  ") is ApprovalMode.SAFE


def test_parse_empty_or_unknown_is_none():
    assert parse_approval_mode("") is None
    assert parse_approval_mode(None) is None
    assert parse_approval_mode("bogus") is None
    assert parse_approval_mode("auto") is None, "the old names are gone"


def test_resolve_defaults_to_ask():
    assert resolve_approval_mode(SimpleNamespace(approval_mode="")) is ApprovalMode.ASK
    assert resolve_approval_mode(SimpleNamespace(approval_mode="interactive")) is ApprovalMode.ASK


def test_resolve_reads_the_mode_set():
    assert resolve_approval_mode(SimpleNamespace(approval_mode="autonomous")) is ApprovalMode.AUTONOMOUS
