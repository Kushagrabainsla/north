"""Environment-file operations used by first-run provider setup."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


def load_env_keys(env_file: Path) -> dict[str, str]:
    """Parse non-empty ``KEY=value`` entries, returning an empty mapping when absent."""
    if not env_file.exists():
        return {}
    keys: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            keys[key.strip()] = value.strip()
    return keys


def update_env_file(env_file: Path, env_key: str, value: str) -> None:
    """Replace or append an environment key and expose it to this process."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    prefix = f"{env_key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{env_key}={value}"
            break
    else:
        lines.append(f"{env_key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[env_key] = value


def save_provider_key(env_file: Path, env_key: str, api_key: str) -> None:
    """Persist a provider key in the environment file and current process."""
    update_env_file(env_file, env_key, api_key)


def parse_provider_selection[Provider](raw: str, providers: list[Provider]) -> list[Provider]:
    """Parse comma-separated 1-based provider indexes in first-seen order."""
    selected: list[Provider] = []
    seen: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        try:
            index = int(part) - 1
        except ValueError:
            continue
        if 0 <= index < len(providers) and index not in seen:
            seen.add(index)
            selected.append(providers[index])
    return selected


def provider_is_configured(
    provider: dict[str, str],
    env_keys: dict[str, str],
    oauth_configured: Callable[[str], bool],
) -> bool:
    """Return whether a provider is available through OAuth, environment, or .env."""
    if provider["auth_kind"] == "oauth_pkce":
        return bool(oauth_configured(provider["id"]))
    return bool(os.environ.get(provider["env_key"], "").strip() or env_keys.get(provider["env_key"], "").strip())


def any_provider_configured(
    providers: list[dict[str, str]], env_keys: dict[str, str], oauth_configured: Callable[[str], bool]
) -> bool:
    """Return whether any configured provider can be used."""
    return any(provider_is_configured(provider, env_keys, oauth_configured) for provider in providers)
