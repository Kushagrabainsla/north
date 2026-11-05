"""north's .env: where the secrets you give the dashboard are kept, readable only by you.

Only the .env in north's home is read (config/settings.py), and agents' file tools never
reach it (CODING_STYLE §16.1.1). Every route that saves a secret writes it through here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# A value of only these characters is written as it is; any other is quoted.
_PLAIN = re.compile(r"[A-Za-z0-9_\-.:@,+/=%]*")


def unstorable(value: str) -> str:
    """Why *value* cannot be kept in .env exactly as given, or "" when it can.

    python-dotenv, which reads the file back, ends a value at a line break, expands
    "${NAME}" even inside quotes, and misreads a quoted value ending in a backslash.
    """
    if "\n" in value or "\r" in value:
        return "it has a line break"
    if "${" in value:
        return 'it contains "${"'
    if value.endswith("\\"):
        return "it ends with a backslash"
    return ""


def write_env_key(home: Path, env_key: str, value: str) -> None:
    """Set `env_key` to *value* in <home>/.env, keeping its other lines and its private permissions.

    Blocking: call it through to_thread so a request handler never stalls the loop.
    Raises ValueError when *value* cannot be kept exactly (see `unstorable`).
    """
    if why := unstorable(value):
        raise ValueError(f"{env_key} cannot be kept in north's .env: {why}.")
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{env_key}="
    replacement = f"{prefix}{_written(value)}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


def remove_env_key(home: Path, env_key: str) -> None:
    """Drop `env_key` from <home>/.env, keeping its other lines and its private permissions."""
    env_path = home / ".env"
    if not env_path.exists():
        return
    prefix = f"{env_key}="
    lines = [line for line in env_path.read_text(encoding="utf-8").splitlines() if not line.startswith(prefix)]
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    env_path.chmod(0o600)


def _written(value: str) -> str:
    """*value* as python-dotenv reads it back unchanged: plain, or double-quoted with \\ and " escaped.

    A password such as `p@ss w#rd` was cut at the `#`, read as a comment.
    """
    if _PLAIN.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
