"""Tests for PatchFileTool CAS conflict detection on concurrent modifications."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.mode import ApprovalMode
from approval.models import ApprovalDecision, Card, CardType
from approval.unattended import UnattendedPolicy
from tools.models import ToolInput
from tools.specialized.patch_file import PatchFileTool


@pytest.mark.asyncio
async def test_patch_file_rejects_concurrent_modification(tmp_path: Path):
    f = tmp_path / "concurrent.py"
    f.write_text("initial_value = 10\n")

    store = MagicMock()
    # Simulate a user taking time to approve, while another process modifies the file
    async def simulate_concurrent_edit(card_id, timeout=300.0):
        # Modify the file on disk behind the tool's back before approval returns
        f.write_text("initial_value = 999\n")
        return Card(
            id=card_id,
            type=CardType.APPROVAL,
            task_id="t1",
            agent="patch_file",
            title="Edit",
            message="msg",
            status=ApprovalDecision.APPROVED,
        )

    store.wait_for_decision = AsyncMock(side_effect=simulate_concurrent_edit)
    tool = PatchFileTool(
        approval_store=store,
        unattended=UnattendedPolicy(),
        mode_provider=lambda: ApprovalMode.INTERACTIVE,
    )

    out = await tool.run(
        ToolInput(
            params={
                "path": str(f),
                "old_string": "initial_value = 10",
                "new_string": "initial_value = 20",
                "workspace": str(tmp_path),
            }
        )
    )

    assert not out.success
    assert "modified concurrently on disk" in out.error
    assert f.read_text() == "initial_value = 999\n"  # Concurrent modification preserved!


@pytest.mark.asyncio
async def test_patch_file_handles_divider_lines_in_search_blocks(tmp_path: Path):
    f = tmp_path / "divider.py"
    content = (
        "# ==========================================\n"
        "# Section 1: Configuration\n"
        "# ==========================================\n"
        "DEBUG = False\n"
    )
    f.write_text(content)

    tool = PatchFileTool()
    block = (
        "<<<<<<< SEARCH\n"
        "# ==========================================\n"
        "# Section 1: Configuration\n"
        "# ==========================================\n"
        "DEBUG = False\n"
        "=======\n"
        "# ==========================================\n"
        "# Section 1: Configuration (Updated)\n"
        "# ==========================================\n"
        "DEBUG = True\n"
        ">>>>>>> REPLACE"
    )
    out = await tool.run(
        ToolInput(
            params={
                "path": str(f),
                "new_string": block,
                "workspace": str(tmp_path),
            }
        )
    )

    assert out.success
    assert "DEBUG = True" in f.read_text()
    assert "Section 1: Configuration (Updated)" in f.read_text()
