"""North says when its own sandbox stopped a command (issue #82, item 8).

Deletes in ~/Downloads and a call to the Docker socket failed with "Operation not
permitted", and north told the user to grant Full Disk Access. The refusal was
north's own sandbox: it lets a command write only in its folder, temp and cache
folders, and it closes unix sockets. `bash` added its hint only when the whole
command failed; `cd ~/Downloads; rm ...; df -h` passed, so no hint came. Now
north explains every refusal it can see, on success too, path by path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tools.specialized import _seatbelt
from tools.specialized._seatbelt import explain


@pytest.fixture
def home(tmp_path, move_north_home, monkeypatch):
    """A home folder with north's home inside it, as on a real Mac.

    It lives in the temp folder, which the box lets commands write; here temp is not writable.
    """
    monkeypatch.setattr(_seatbelt, "_TEMP_DIRS", ())
    user = tmp_path / "user"
    (user / "Downloads").mkdir(parents=True)
    (user / ".ssh").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    north = move_north_home(user / ".north")
    (north / "tasks" / "t1").mkdir(parents=True)
    (north / "notes" / "t1").mkdir(parents=True)
    return user


def test_a_delete_outside_the_folder_says_it_was_north_and_how_to_allow_it(home) -> None:
    task = home / ".north" / "tasks" / "t1"
    target = home / "Downloads" / "Roblox.dmg"

    note = explain(f"rm: {target}: Operation not permitted\n", str(task))

    assert "North's own sandbox refused" in note
    assert "not a macOS permission" in note
    assert "Full Disk Access" in note
    assert f"`{target}` is outside the folders this command may write" in note
    assert f'write_outside=["{home / "Downloads"}"]' in note


def test_a_credential_folder_is_closed_whatever_the_answer(home) -> None:
    key = home / ".ssh" / "id_ed25519"

    note = explain(f"cat: {key}: Operation not permitted", None)

    assert f"`{key}` holds credentials" in note
    assert "No answer opens it" in note
    assert "write_outside" not in note


def test_north_s_own_notes_can_be_read_but_not_written(home) -> None:
    note_file = home / ".north" / "notes" / "t1" / "probe.md"

    note = explain(f"rm: {note_file}: Operation not permitted", str(home / ".north" / "tasks" / "t1"))

    assert "may read it but not write it" in note
    assert "write_file" in note


def test_a_unix_socket_is_named_as_closed(home) -> None:
    socket = f"{home}/.docker/run/docker.sock"
    encoded = socket.replace("/", "%2F")
    output = (
        f"permission denied while trying to connect to the Docker daemon socket at unix://{socket}: "
        f'Get "http://{encoded}/v1.47/system/df": dial unix {socket}: connect: operation not permitted'
    )

    note = explain(output, None)

    assert f"`{socket}`" in note
    assert note.count("is a unix socket") == 1, "the URL-encoded copy of the path is not a second socket"
    assert "unix sockets" in note.lower()
    assert "No answer opens" in note


def test_a_relative_path_falls_back_to_the_general_rule(home) -> None:
    """`cd ~/Downloads; rm x.dmg` names x.dmg only: north cannot tell the folder, so it states the rule."""
    task = home / ".north" / "tasks" / "t1"

    note = explain("rm: Wallper.dmg: Operation not permitted", str(task))

    assert "Full Disk Access" in note
    assert f"`{task}`" in note
    assert "write_outside" in note


def test_a_refusal_inside_the_folder_is_not_blamed_on_north(home) -> None:
    """A path north lets the command write was refused by something else: north does not claim it."""
    task = home / ".north" / "tasks" / "t1"

    note = explain(f"touch: {task}/x: Operation not permitted", str(task))

    assert "North's own sandbox refused" not in note


def test_output_with_no_refusal_gets_no_note(home) -> None:
    assert explain("total 0\nall good\n", None) == ""


@pytest.mark.skipif(not _seatbelt.available(), reason="needs macOS Seatbelt")
async def test_a_passing_command_that_was_refused_part_way_still_says_so(tmp_path) -> None:
    """The case from the logs: the refused delete is followed by a command that passes."""
    from tests.conftest import approving_store, bind_approvals
    from tools.models import ToolInput
    from tools.specialized.bash import BashTool

    folder = tmp_path / "work"
    folder.mkdir()
    # Not in a temp folder, which the box lets every command write.
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]) as kept:
        outside = Path(kept)
        (outside / "old.dmg").write_text("x")
        tool = bind_approvals(BashTool(os_sandbox=True), store=approving_store())

        out = await tool.execute(
            ToolInput(
                params={"command": f"rm {outside / 'old.dmg'}; echo done", "task_id": "t"},
                granted_workspace=str(folder),
            )
        )

        assert out.success, out.error
        assert (outside / "old.dmg").exists()
    assert "North's own sandbox refused" in out.data["stdout"]
    assert f"`{outside / 'old.dmg'}` is outside the folders this command may write" in out.data["stdout"]
    assert "write_outside" in out.data["stdout"]
