"""Tests for the single-source version (review finding R3#19)."""

from __future__ import annotations

import re

from utils.version import NORTH_VERSION


def test_version_is_populated() -> None:
    assert re.match(r"^\d+\.\d+", NORTH_VERSION) or NORTH_VERSION == "0.0.0+unknown"


def test_fastapi_apps_use_single_source_version() -> None:
    from approval.callback_server import app as callback_app
    from orchestrator.app import app as orchestrator_app

    assert orchestrator_app.version == NORTH_VERSION
    assert callback_app.version == NORTH_VERSION
