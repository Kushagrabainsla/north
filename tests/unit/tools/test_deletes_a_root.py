"""Only a recursive delete of a root folder is refused outright; deleting one named folder asks.

Issue #76, A12: bash looked for the text "rm -rf /", so `rm -rf $C/nuke_dir/sub`
was "recognised as catastrophic" and refused unseen, even after the user approved
the delete, in ask and safe mode alike, while `rm -rf ~`, `rm -fr /` and
`sudo rm -rf /usr` were not recognised at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._path import deletes_a_root
from tools.models import ToolInput
from tools.specialized.bash import BashTool


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -fr /",
        "rm -r -f /*",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf $HOME/*",
        "sudo rm -rf /usr",
        "cd /tmp && rm --recursive --force /etc",
        "rm -rf / --no-preserve-root",
    ],
)
def test_a_recursive_delete_of_a_root_folder_is_recognised(command: str) -> None:
    assert deletes_a_root(command)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /home/sam/projects/nuke_dir/sub",
        "rm -rf ./build",
        "rm -f /etc/hosts.bak",  # not recursive
        "echo 'rm -rf /'",  # text, not a delete
        "ls -la /",
    ],
)
def test_deleting_a_named_folder_is_not(command: str) -> None:
    assert not deletes_a_root(command)


async def test_bash_states_the_fact_for_the_folder_and_not_for_root(tmp_path: Path) -> None:
    tool = BashTool()
    named = await tool.describe(
        ToolInput(params={"command": f"rm -rf {tmp_path}/nuke_dir/sub"}, granted_workspace=str(tmp_path))
    )
    root = await tool.describe(ToolInput(params={"command": "rm -rf ~"}, granted_workspace=str(tmp_path)))

    assert named is not None and not named.action.obviously_destructive
    assert root is not None and root.action.obviously_destructive
