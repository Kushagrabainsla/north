"""Tests for PatchFileTool unattended-mode auto-approval (in-workspace edits)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.mode import ApprovalMode
from approval.unattended import UnattendedPolicy
from tools.models import ToolInput
from tools.specialized.patch_file import PatchFileTool


@pytest.mark.asyncio
async def test_unattended_applies_in_workspace_edit_without_card(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("value = 1\n")
    store = MagicMock()
    store.wait_for_decision = AsyncMock()  # must never be called
    tool = PatchFileTool(approval_store=store, unattended=UnattendedPolicy(), mode_provider=lambda: ApprovalMode.AUTO)

    out = await tool.run(
        ToolInput(
            params={
                "path": str(f),
                "old_string": "value = 1",
                "new_string": "value = 2",
                "workspace": str(tmp_path),
            }
        )
    )
    assert out.success, out.error
    assert f.read_text() == "value = 2\n"
    store.wait_for_decision.assert_not_awaited()  # no approval card surfaced


@pytest.mark.asyncio
async def test_unattended_disabled_still_gates(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("value = 1\n")
    store = MagicMock()
    # reject any surfaced decision
    store.wait_for_decision = AsyncMock(return_value=None)
    tool = PatchFileTool(
        approval_store=store, unattended=UnattendedPolicy(), mode_provider=lambda: ApprovalMode.INTERACTIVE
    )

    out = await tool.run(
        ToolInput(
            params={
                "path": str(f),
                "old_string": "value = 1",
                "new_string": "value = 2",
                "workspace": str(tmp_path),
            }
        )
    )
    assert not out.success  # gated -> rejected (no approver)
    assert f.read_text() == "value = 1\n"  # unchanged
