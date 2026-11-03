"""`-w` says what it does: a dashboard session works in the folder picked for it, never the server's.

Issue #76, M7: `north start -w <folder>` promised "Root directory agents can
read/write", and dashboard sessions with no folder picked then worked in their
own task folder: "what files are in my workspace?" got "(empty directory)". A
task never works in the folder north was started in (CODING_STYLE §16.1.1);
`-w` gives the terminal chat its folder and sets where the dashboard's picker
opens.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli import main


@pytest.mark.parametrize("command", ["start", "web"])
def test_the_workspace_option_says_a_dashboard_session_works_where_you_pick(command: str) -> None:
    text = " ".join(CliRunner().invoke(main.app, [command, "--help"], terminal_width=200).output.split())

    assert "folder picker opens" in text
    assert "Root directory agents can read/write" not in text
