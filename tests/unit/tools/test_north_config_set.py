"""`north_config set` says what really happened: applied, waiting for a restart, or refused."""

from __future__ import annotations

import pytest

from config.settings import settings
from tools.specialized.north_config import NorthConfigTool


def _env() -> str:
    path = settings.north_home / ".env"
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def _set(key: str, value: str):
    return await NorthConfigTool()._set({"key": key, "value": value})


@pytest.mark.asyncio
async def test_a_setting_read_at_startup_says_it_waits_for_a_restart() -> None:
    output = await _set("NORTH_AGENT_MAX_ITERATIONS", "7")

    assert output.success and "restarts" in output.data["note"]
    assert "reloaded" not in output.data["note"].lower()


@pytest.mark.asyncio
async def test_a_typo_is_refused_and_nothing_is_written() -> None:
    output = await _set("NORTH_AGENT_MAX_ITERATION", "7")

    assert not output.success and "no setting" in output.error
    assert _env() == ""


@pytest.mark.asyncio
async def test_a_bad_value_is_refused_before_it_can_break_the_next_start() -> None:
    output = await _set("NORTH_AGENT_MAX_ITERATIONS", "many")

    assert not output.success and "not a valid value" in output.error
    assert _env() == ""


@pytest.mark.asyncio
async def test_the_approval_mode_goes_to_the_one_live_setting_not_the_file() -> None:
    tool = NorthConfigTool()

    output = await tool._set({"key": "NORTH_APPROVAL_MODE", "value": "safe"})

    assert output.success and output.data["value"] == "safe"
    assert "NORTH_APPROVAL_MODE" not in _env()


@pytest.mark.asyncio
async def test_a_provider_key_is_still_accepted() -> None:
    output = await _set("FAL_KEY", "abc123")

    assert output.success
    assert "NORTH_FAL_KEY=abc123" in _env()
