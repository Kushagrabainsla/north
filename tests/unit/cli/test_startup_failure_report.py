"""When the server dies on boot, the CLI has to say why.

Both calls that reported a failed startup passed `err=True` to rich's
`Console.print`, which does not take it. So the reporter raised a TypeError
about itself, and a server that failed to boot produced a traceback about the
error handler instead of the error - exactly when the message mattered most.

The real reason was in ~/.north/north.log the whole time, and nothing said so.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import typer

import cli.main as cli

CLI_SOURCE = Path(cli.__file__)


def test_no_console_print_passes_err() -> None:
    """rich decides stderr at construction. `err=` there is always a crash.

    Checked across the module rather than at the two known sites: the same
    mistake is easy to repeat, and it only ever fires on an error path, which is
    where it will not be noticed.
    """
    tree = ast.parse(CLI_SOURCE.read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "print"
        and isinstance(node.func.value, ast.Name)
        and "console" in node.func.value.id.lower()
        and any(kw.arg == "err" for kw in node.keywords)
    ]
    assert not offenders, f"Console.print() does not accept err= (lines {offenders})"


def test_reporting_a_failure_exits_instead_of_raising(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))

    with pytest.raises(typer.Exit) as exit_info:
        cli._report_startup_failure("server process exited unexpectedly (code 3)")

    assert exit_info.value.exit_code == 1
    assert "exited unexpectedly" in capsys.readouterr().err


def test_the_message_points_at_the_log(tmp_path, monkeypatch, capsys) -> None:
    """Reporting that something failed without saying where to look is a long walk."""
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))
    (tmp_path / "north.log").write_text("some earlier line\n")

    with pytest.raises(typer.Exit):
        cli._report_startup_failure("server did not respond in time")

    assert "north.log" in capsys.readouterr().err


def test_the_exception_is_quoted_not_the_noise_after_it(tmp_path) -> None:
    """The last line of the traceback is the answer; the frames are context.

    Matched on the exception rather than the "Traceback" header, because the
    header arrives wrapped in whatever prefix the logger added.
    """
    log = tmp_path / "north.log"
    log.write_text(
        '{"ts": "2026-09-09", "msg": "a structured record that is not the crash"}\n'
        "ERROR:    Traceback (most recent call last):\n"
        '  File "/x/orchestrator/app.py", line 457, in _configure_routers\n'
        "    configure_web(\n"
        "TypeError: configure() got an unexpected keyword argument 'unattended_rules'\n"
        "\n"
        "ERROR:    Application startup failed. Exiting.\n"
    )

    quoted = cli._last_error_lines(log)

    assert any("unexpected keyword argument" in line for line in quoted)
    assert not any(line.lstrip().startswith("{") for line in quoted), "log records are not the crash"


def test_a_log_with_no_traceback_still_says_something(tmp_path) -> None:
    log = tmp_path / "north.log"
    log.write_text("port 8000 already in use\n")
    assert cli._last_error_lines(log) == ["port 8000 already in use"]


def test_a_missing_or_unreadable_log_is_not_a_second_failure(tmp_path) -> None:
    """This runs while reporting a failure; it must never raise one of its own."""
    assert cli._last_error_lines(tmp_path / "does-not-exist.log") == []
