"""The configured timezone is North's one source of local wall-clock truth."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import utils.time as time_utils


def test_configured_timezone_drives_local_conversion(monkeypatch) -> None:
    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)
    time_utils.configure_timezone("America/Los_Angeles")

    local = time_utils.from_epoch(datetime(2026, 9, 13, 19, 0, tzinfo=UTC).timestamp()).astimezone(
        time_utils.local_timezone()
    )

    assert time_utils.local_timezone_name() == "America/Los_Angeles"
    assert (local.hour, local.tzname()) == (12, "PDT")


def test_naive_input_is_read_in_the_configured_timezone(monkeypatch) -> None:
    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)
    time_utils.configure_timezone("America/Los_Angeles")

    epoch = time_utils.parse_local("2026-09-13T12:00")

    assert time_utils.from_epoch(epoch) == datetime(2026, 9, 13, 19, 0, tzinfo=UTC)


def test_unknown_configured_timezone_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)

    with pytest.raises(ValueError, match="Unknown timezone"):
        time_utils.configure_timezone("Mars/Olympus_Mons")


def test_runtime_context_has_minute_precision_and_named_zone(monkeypatch) -> None:
    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)
    time_utils.configure_timezone("America/Los_Angeles")

    rendered = time_utils.runtime_context(datetime(2026, 9, 13, 19, 15, 42, tzinfo=UTC))

    assert "local_time: 2026-09-13T12:15-07:00" in rendered
    assert "timezone: America/Los_Angeles" in rendered
    assert ":42" not in rendered
