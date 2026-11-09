"""`north reset` wipes north's data, keeps your keys, and leaves alone what it does not recognise."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from typer.testing import CliRunner

from cli.main import _RESET_KEEPS, _RESET_WIPES, app
from config.settings import settings

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_DIRS = (
    "agents",
    "approval",
    "bootstrap",
    "cli",
    "coding_agents",
    "config",
    "context",
    "flows",
    "gateways",
    "inference",
    "jobs",
    "ledger",
    "mcp",
    "memory",
    "orchestrator",
    "skills",
    "tools",
    "utils",
    "web",
)
_HOME_ENTRY_RE = re.compile(r"north_home\s*/\s*\"([^\"]+)\"")


def _reset() -> str:
    result = CliRunner().invoke(app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    return result.output


def test_saved_secrets_survive_and_data_does_not() -> None:
    home = settings.north_home
    (home / "secrets.json").write_text("{}", encoding="utf-8")
    (home / ".env").write_text("KEY=value\n", encoding="utf-8")
    (home / "credentials").mkdir()
    (home / "credentials" / "codex.json").write_text("{}", encoding="utf-8")
    (home / "ledger.db").write_text("x", encoding="utf-8")
    (home / "ledger.db-wal").write_text("x", encoding="utf-8")
    (home / "tasks").mkdir()
    (home / "tasks" / "t.md").write_text("x", encoding="utf-8")

    _reset()

    assert (home / "secrets.json").exists()
    assert (home / ".env").exists()
    assert (home / "credentials" / "codex.json").exists()
    assert not (home / "ledger.db").exists()
    assert not (home / "ledger.db-wal").exists()
    assert not (home / "tasks").exists()


def test_something_it_does_not_recognise_survives_and_is_named() -> None:
    home = settings.north_home
    (home / "my_notes.md").write_text("mine", encoding="utf-8")

    output = _reset()

    assert (home / "my_notes.md").read_text(encoding="utf-8") == "mine"
    assert "my_notes.md" in output


def test_a_file_mode_survives_because_nothing_is_moved() -> None:
    home = settings.north_home
    secrets = home / "secrets.json"
    secrets.touch(mode=0o600)

    _reset()

    assert secrets.stat().st_mode & 0o777 == 0o600


def test_every_file_north_writes_in_its_home_is_classified() -> None:
    """A new file nobody classified would survive every reset as "not recognised"."""
    names: set[str] = set()
    for folder in _SOURCE_DIRS:
        for path in (_ROOT / folder).rglob("*.py"):
            names.update(_HOME_ENTRY_RE.findall(path.read_text(encoding="utf-8")))

    unclassified = {
        name
        for name in names
        if name not in _RESET_KEEPS and not any(fnmatch.fnmatch(name, pattern) for pattern in _RESET_WIPES)
    }

    assert not unclassified
