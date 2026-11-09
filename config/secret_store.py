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
import functools
import json
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import dotenv_values

from config.settings import settings

FILE_NAME = "secrets.json"
# The other places in north's home that hold a credential, named by a key ending like these.
_ENV_FILE_NAME = ".env"
_SERVER_KEY_NAME = "secret.key"
_LOGINS_DIR_NAME = "credentials"
_CREDENTIAL_KEY_RE = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|passwd)$")
_MIN_CREDENTIAL_LENGTH = 8
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
        self._home = home
        self._path = home / FILE_NAME
        self._mask = _Mask((), None, {})

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
        """*value* with every known credential taken out of each string in it, at any depth; the same object when none.

        Known means your secrets, and the keys and logins in north's home: an error from a
        provider's client quotes the key it sent, and the model and the log must not read it.
        """
        mask = self._current_mask()
        return _hidden(value, mask) if mask.pattern else value

    def _current_mask(self) -> _Mask:
        """The mask for what the files hold now, rebuilt only when one of them changed: logs call this per line."""
        stamp = self._stamp()
        if stamp != self._mask.stamp:
            self._mask = _Mask.of(stamp, self._known())
        return self._mask

    def _credential_files(self) -> list[Path]:
        return [
            self._path,
            self._home / _ENV_FILE_NAME,
            self._home / _SERVER_KEY_NAME,
            *sorted((self._home / _LOGINS_DIR_NAME).glob("*")),
        ]

    def _stamp(self) -> tuple[tuple[str, int, int], ...]:
        stamps = []
        for path in self._credential_files():
            with contextlib.suppress(OSError):
                status = path.stat()
                stamps.append((str(path), status.st_mtime_ns, status.st_size))
        return tuple(stamps)

    def _known(self) -> dict[str, str]:
        """Every credential north holds, as {value: the label that replaces it}."""
        known = {
            str(entry["value"]): f"[secret {name}]"
            for name, entry in self._entries().items()
            if len(str(entry["value"])) >= MIN_VALUE_LENGTH
        }
        found = list(_labelled(dotenv_values(self._home / _ENV_FILE_NAME)))
        with contextlib.suppress(OSError):
            found.append(("north_server_key", (self._home / _SERVER_KEY_NAME).read_text(encoding="utf-8").strip()))
        for path in sorted((self._home / _LOGINS_DIR_NAME).glob("*")):
            with contextlib.suppress(OSError, ValueError):
                found.extend(_labelled(json.loads(path.read_text(encoding="utf-8"))))
        for name, value in found:
            if len(value) >= _MIN_CREDENTIAL_LENGTH:
                known.setdefault(value, f"[secret {name}]")
        return known

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


@dataclass(frozen=True)
class _Mask:
    """One pattern for every known credential, in each form it reaches text in."""

    stamp: tuple[tuple[str, int, int], ...]
    pattern: re.Pattern[str] | None
    labels: dict[str, str]

    @classmethod
    def of(cls, stamp: tuple[tuple[str, int, int], ...], known: dict[str, str]) -> _Mask:
        # Longest first: a credential inside a longer one must not leave the longer one's tail behind.
        ordered = sorted(known, key=len, reverse=True)
        labels = {f"s{index}": known[value] for index, value in enumerate(ordered)}
        alternatives = (f"(?P<s{index}>{_any_form(value)})" for index, value in enumerate(ordered))
        return cls(stamp, re.compile("|".join(alternatives)) if ordered else None, labels)

    def hide(self, text: str) -> str:
        if self.pattern is None:
            return text
        return self.pattern.sub(lambda match: self.labels[str(match.lastgroup)], text)


def _any_form(value: str) -> str:
    """A pattern for *value* as written, URL-encoded (either hex case) or JSON-escaped.

    Exact-text matching missed a key with `+` or `/` once httpx quoted it in an error's URL, and
    which characters a URL leaves alone depends on the part of the URL, so each one is optional.
    """
    return "".join(_char_forms(char) for char in value)


def _char_forms(char: str) -> str:
    forms = {re.escape(char), re.escape(json.dumps(char)[1:-1])}
    encoded = quote(char, safe="")
    if encoded != char:
        forms.add(f"(?i:{encoded})")
    if char == " ":
        forms.add(r"\+")
    return f"(?:{'|'.join(sorted(forms))})" if len(forms) > 1 else forms.pop()


def _labelled(data: Any) -> Iterator[tuple[str, str]]:
    """Each (key, value) in *data*, at any depth, whose key names a credential."""
    if isinstance(data, list):
        for item in data:
            yield from _labelled(item)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and _CREDENTIAL_KEY_RE.search(str(key)):
                yield str(key), value
            else:
                yield from _labelled(value)


def _hidden(value: Any, mask: _Mask) -> Any:
    if isinstance(value, str):
        return mask.hide(value)
    if isinstance(value, dict):
        return {key: item if key in _IMAGE_KEYS else _hidden(item, mask) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_hidden(item, mask) for item in value)
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


@functools.lru_cache(maxsize=8)
def _store_at(home: Path) -> SecretStore:
    return SecretStore(home)


def store() -> SecretStore:
    """The secrets of this north, in its home. One object per home, so its mask is built once."""
    return _store_at(settings.north_home)


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
