"""Your keys and passwords, which north's tools use by name (CODING_STYLE §7.6).

The model knows a secret by its name only. A tool that needs one declares it
(`uses_secrets`), the approval card says so, and the tool reads the value inside
its own `run()` with `secret(name)`. `Tool.execute` releases only the names a tool
declared, and takes every stored value back out of what any tool returns before
the model reads it. You add, replace and remove secrets on the dashboard; no tool
can. They are kept in north's home, in `secrets.json`, readable only by you, which
agents' file tools never reach (CODING_STYLE §16.1.1).
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings

FILE_NAME = "secrets.json"
# A name the model and the user can both say: lowercase letters, digits and underscores.
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# As short as a TP-Link password may be. Images are left alone when values are taken out of
# tool output (`_IMAGE_KEYS`): a short value turns up inside a screenshot's base64 by chance.
MIN_VALUE_LENGTH = 6
# Where a tool returns an image: the model sees it as a picture, never as text.
_IMAGE_KEYS = frozenset({"base64_image"})
MAX_VALUE_LENGTH = 8192

# The tool running now and the names it declared: what `secret()` may release.
_released: ContextVar[tuple[str, frozenset[str]] | None] = ContextVar("north_released_secrets", default=None)


class SecretUnavailable(LookupError):
    """A secret a tool asked for that north will not give it, with what to do instead."""


@dataclass(frozen=True)
class SavedSecret:
    """A secret as anyone but the tool that uses it sees it: no value."""

    name: str
    note: str
    updated_at: str


class SecretStore:
    """The secrets kept in *home*. Every read and write of `secrets.json` goes through here."""

    def __init__(self, home: Path) -> None:
        self._path = home / FILE_NAME

    def saved(self) -> list[SavedSecret]:
        """Every secret's name, note and when it was last set, by name."""
        return [
            SavedSecret(name, str(entry.get("note") or ""), str(entry.get("updated_at") or ""))
            for name, entry in sorted(self._entries().items())
        ]

    def names(self) -> set[str]:
        return set(self._entries())

    def save(self, name: str, value: str, note: str = "") -> None:
        """Keep *value* under *name*, replacing any value it had.

        Raises ValueError, saying why, for a name or value north cannot keep (see `unusable`).
        """
        if why := unusable(name, value):
            raise ValueError(why)
        entries = self._entries()
        entries[name] = {"value": value, "note": note.strip(), "updated_at": datetime.now(UTC).isoformat()}
        self._write(entries)

    def remove(self, name: str) -> bool:
        """Forget *name*; False when there was no such secret."""
        entries = self._entries()
        if entries.pop(name, None) is None:
            return False
        self._write(entries)
        return True

    def value(self, name: str) -> str | None:
        entry = self._entries().get(name)
        return str(entry["value"]) if entry else None

    def hide(self, text: str) -> str:
        """*text* with every stored value replaced by its name, as "[secret openweather]"."""
        return self.hide_in(text)

    def hide_in(self, value: Any) -> Any:
        """*value* with every stored value taken out of each string in it, at any depth; the same object when none."""
        known = [
            (str(entry["value"]), f"[secret {name}]")
            for name, entry in self._entries().items()
            if len(str(entry["value"])) >= MIN_VALUE_LENGTH
        ]
        return _hidden(value, known) if known else value

    def _entries(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {name: entry for name, entry in data.items() if isinstance(entry, dict) and "value" in entry}

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        partial = self._path.with_suffix(".json.partial")
        # Created private, so the value is never readable by others, even for an instant.
        partial.touch(mode=0o600, exist_ok=True)
        partial.chmod(0o600)
        partial.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
        partial.replace(self._path)


def _hidden(value: Any, known: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for secret_value, label in known:
            value = value.replace(secret_value, label)
        return value
    if isinstance(value, dict):
        return {key: item if key in _IMAGE_KEYS else _hidden(item, known) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_hidden(item, known) for item in value)
    return value


def unusable(name: str, value: str) -> str:
    """Why north cannot keep *value* as the secret *name*, or "" when it can."""
    if not NAME_RE.fullmatch(name):
        return "A secret's name is lowercase letters, digits and underscores, starting with a letter."
    if len(value) < MIN_VALUE_LENGTH:
        return f"A secret is at least {MIN_VALUE_LENGTH} characters."
    if len(value) > MAX_VALUE_LENGTH:
        return f"A secret is at most {MAX_VALUE_LENGTH} characters."
    return ""


def store() -> SecretStore:
    """The secrets of this north, in its home."""
    return SecretStore(settings.north_home)


@contextlib.contextmanager
def released_to(tool: str, names: tuple[str, ...]) -> Iterator[None]:
    """While a call of *tool* runs, `secret()` gives it the names it declared, and no others."""
    token = _released.set((tool, frozenset(names)))
    try:
        yield
    finally:
        _released.reset(token)


def secret(name: str) -> str:
    """The value of your secret *name*, for the tool running now, which must declare it in `uses_secrets`.

    Raises SecretUnavailable, saying what to do, for a name the tool did not declare or
    one that is not saved.
    """
    running = _released.get()
    if running is None or name not in running[1]:
        tool = running[0] if running else "code outside a tool"
        raise SecretUnavailable(
            f"{tool} did not declare the secret {name!r}: add it to the tool's `uses_secrets`, "
            "so the approval card shows it and north gives it to this tool."
        )
    value = store().value(name)
    if value is None:
        raise SecretUnavailable(
            f"North has no secret named {name!r}. Ask the user to add it on the dashboard "
            f"(Settings, Secrets) under the name {name!r}; never ask for its value in chat."
        )
    return value
