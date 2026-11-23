"""Every `north` command is on the landing page, and every command there is one north has.

The command reference moved from the README to the landing page (`site/index.html`, Commands),
its one home. Nothing ever checked the README's list against the CLI; this checks the page's
both ways, so a new command cannot go unlisted and a removed one cannot stay.

The guides (`guides/src/content/docs/`) name commands too: each one, and each of its options,
must be one north has, so a renamed command cannot leave a guide with old steps.
"""

from __future__ import annotations

import inspect
import re
from html.parser import HTMLParser
from pathlib import Path

import click
from typer.main import get_command

from cli.main import app

ROOT = Path(__file__).parents[3]
SITE = ROOT / "site" / "index.html"
GUIDES = ROOT / "guides" / "src" / "content" / "docs"
# A command in a guide: a line of a code block, or `inline code`, that starts with "north".
CODE_BLOCK = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
BLOCK_COMMAND = re.compile(r"^(north(?: .*)?)$", re.MULTILINE)
INLINE_COMMAND = re.compile(r"`(north(?: [^`]*)?)`")


def _path(text: str) -> str:
    """A command's path, as `cron add`: the words after `north` until an option or argument."""
    path = []
    for word in text.split()[1:]:
        if not re.fullmatch(r"[a-z][a-z-]*", word):
            break
        path.append(word)
    return " ".join(path)


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
    return {_path(text) for text in parser.listed}


def _commands() -> dict[str, click.Command]:
    """Every command path north has, with its click command; a group that runs bare counts too."""
    found: dict[str, click.Command] = {}

    def walk(command: click.Command, path: str) -> None:
        if isinstance(command, click.Group):
            if path and command.invoke_without_command and command.callback:
                found[path] = command
            for name, sub in command.commands.items():
                if not sub.hidden:
                    walk(sub, f"{path} {name}".strip())
        elif command.callback:
            found[path] = command

    root = get_command(app)
    walk(root, "")
    found.setdefault("", root)  # `north` itself
    return found


def _cli() -> dict[str, object]:
    """Every command path north has, with the function it runs."""
    return {path: inspect.unwrap(command.callback) for path, command in _commands().items() if command.callback}


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


def _in_the_guides() -> list[str]:
    """Each `north ...` command the guides name, as written."""
    found = []
    for page in sorted(GUIDES.rglob("*.md*")):
        text = page.read_text(encoding="utf-8")
        for block in CODE_BLOCK.findall(text):
            found += BLOCK_COMMAND.findall(block)
        found += INLINE_COMMAND.findall(CODE_BLOCK.sub("", text))
    return [command.strip() for command in found]


def test_every_command_in_the_guides_is_one_north_has() -> None:
    commands = _commands()
    named = _in_the_guides()
    wrong = []
    for text in named:
        command = commands.get(_path(text))
        if command is None:
            wrong.append(text)
            continue
        options = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
        wrong += [f"{text}: {word}" for word in text.split() if word.startswith("-") and word not in options]

    assert len(named) >= 2  # the Quickstart names `north web` and `north`
    assert wrong == []
