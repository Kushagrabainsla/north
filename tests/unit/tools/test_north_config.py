"""north_config: reading north's settings never asks; changing them is a change to north itself."""

from __future__ import annotations

import pytest

from approval.approvals import Approvals
from tools.models import ToolInput
from tools.specialized.north_config import NorthConfigTool


@pytest.mark.parametrize(
    "params",
    [{"action": "list"}, {"action": "get", "key": "NORTH_LOG_LEVEL"}, {"action": "autonomy"}, {"action": "power"}],
)
def test_reading_north_settings_is_not_a_change(params) -> None:
    """A plain read of the mode needed a card, and in autonomous the decider refused it (issue #76, A17)."""
    assert NorthConfigTool().mutates(params) is False


async def test_reading_the_current_autonomy_runs_with_nobody_to_ask() -> None:
    tool = NorthConfigTool()
    tool.approvals = Approvals.unbound()  # anything that would ask is refused

    output = await tool.execute(ToolInput(params={"action": "autonomy"}))

    assert output.success, output.error


@pytest.mark.parametrize(
    "params",
    [
        {"action": "autonomy", "value": "yolo"},
        {"action": "power", "value": "sport"},
        {"action": "set", "key": "NORTH_LOG_LEVEL", "value": "DEBUG"},
    ],
)
async def test_changing_north_settings_is_a_change_to_north_itself(params) -> None:
    """Never replayed, and below yolo only a person decides it (issue #76, A3)."""
    request = await NorthConfigTool().describe(ToolInput(params=params))

    assert NorthConfigTool().mutates(params) is True
    assert request.action.changes_north is True


async def test_a_secret_stays_off_the_card_and_out_of_the_remembered_identity() -> None:
    value = "sk-ant-test-0123456789abcdef"
    params = {"action": "set", "key": "NORTH_ANTHROPIC_API_KEY", "value": value}

    request = await NorthConfigTool().describe(ToolInput(params=params))

    assert value not in request.message
    assert value not in request.action.describe()
