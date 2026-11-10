"""How the last update went is kept on disk, so the dashboard can say so after the restart."""

from __future__ import annotations

import subprocess
import sys

from utils.update_record import RECORD_NAME, last_update, record_update


def test_nothing_recorded_reads_as_never_updated(tmp_path) -> None:
    assert last_update(tmp_path) is None


def test_the_last_state_written_is_the_one_read(tmp_path) -> None:
    record_update(tmp_path, "running")
    record_update(tmp_path, "failed", "Install failed:\nno network")

    last = last_update(tmp_path)

    assert last is not None
    assert last["state"] == "failed" and last["detail"] == "Install failed:\nno network"


def test_a_running_update_whose_process_is_gone_reads_as_failed(tmp_path) -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    record_update(tmp_path, "running", pid=finished.pid)

    last = last_update(tmp_path)

    assert last is not None and last["state"] == "failed"
    assert "stopped before it finished" in last["detail"]


def test_a_running_update_that_is_still_running_reads_as_running(tmp_path) -> None:
    record_update(tmp_path, "running")  # this test's own process, alive

    last = last_update(tmp_path)

    assert last is not None and last["state"] == "running"


def test_an_unreadable_record_reads_as_never_updated(tmp_path) -> None:
    (tmp_path / RECORD_NAME).write_text("{not json", encoding="utf-8")
    assert last_update(tmp_path) is None

    (tmp_path / RECORD_NAME).write_text('{"state": "exploded"}', encoding="utf-8")
    assert last_update(tmp_path) is None
