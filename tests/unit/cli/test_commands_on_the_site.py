"""Every `north` command is on the landing page, and every command there is one north has.

The command reference moved from the README to the landing page (`site/index.html`, Commands),
its one home. Nothing ever checked the README's list against the CLI; this checks the page's
both ways, so a new command cannot go unlisted and a removed one cannot stay.
"""

from __future__ import annotations

import inspect
import re
from html.parser import HTMLParser
from pathlib import Path

import click
from typer.main import get_command

from cli.main import app

SITE = Path(__file__).parents[3] / "site" / "index.html"


class _Commands(HTMLParser):
    """The commands the page's Commands section names: each `north ...` in a <code>, in a row's term or its text."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[str] = []
        self._depth = 0  # inside <section id="commands">, counted by nesting
        self._in_code = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth:
            self._depth += tag == "section"
        elif tag == "section" and dict(attrs).get("id") == "commands":
            self._depth = 1
        self._in_code = bool(self._depth) and tag == "code"

    def handle_endtag(self, tag: str) -> None:
        if self._depth and tag == "section":
            self._depth -= 1
        if tag == "code":
            self._in_code = False

    def handle_data(self, data: str) -> None:
        if self._in_code and data.strip().split()[:1] == ["north"]:
            self.listed.append(data.strip())


def _listed() -> set[str]:
    """Each listed command's path, as `cron add`: the words after `north` until an option or argument."""
    parser = _Commands()
    parser.feed(SITE.read_text(encoding="utf-8"))
    paths = set()
    for text in parser.listed:
        words = text.split()[1:]
        path = []
        for word in words:
            if not re.fullmatch(r"[a-z][a-z-]*", word):
                break
            path.append(word)
        paths.add(" ".join(path))
    return paths


def _cli() -> dict[str, object]:
    """Every command path north has, with the function it runs; a group that runs bare counts too."""
    found: dict[str, object] = {}

    def walk(command: click.Command, path: str) -> None:
        if isinstance(command, click.Group):
            if path and command.invoke_without_command and command.callback:
                found[path] = inspect.unwrap(command.callback)
            for name, sub in command.commands.items():
                if not sub.hidden:
                    walk(sub, f"{path} {name}".strip())
        elif command.callback:
            found[path] = inspect.unwrap(command.callback)

    walk(get_command(app), "")
    return found


def test_every_command_on_the_page_is_one_north_has() -> None:
    listed, real = _listed(), _cli()

    assert len(listed) > 30
    assert listed - set(real) - {""} == set()  # "" is `north` itself, and its --yolo


def test_every_command_north_has_is_on_the_page() -> None:
    """A command with two names (`north models` and `north inference models`) is listed once, under either."""
    listed, real = _listed(), _cli()
    names: dict[object, set[str]] = {}
    for path, function in real.items():
        names.setdefault(function, set()).add(path)

    missing = sorted(min(paths) for paths in names.values() if not paths & listed)
    assert missing == []
