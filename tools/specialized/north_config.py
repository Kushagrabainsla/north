"""Manage north's own configuration — read and update .env settings.

Agents can list config keys, get a specific value, or set (add/update) a
key-value pair. Write operations go through the approval gate so the user
sees exactly what key/value is being changed.

This is how north self-configures: "add a provider key" → agent calls
north_config set NORTH_OPENCODE_ZEN_API_KEY=xxx, and the change takes effect
immediately (the inference router is rebuilt in place — no restart needed).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from inference.registry import PROVIDER_DEFINITIONS
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


def _upsert_env_key(path: Path, key: str, value: str) -> None:
    """Set `key=value` in the .env at *path*, replacing any existing line.

    Blocking - call via to_thread.
    """
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*", re.MULTILINE)
    if pattern.search(current):
        new_text = pattern.sub(f"{key}={value}", current)
    else:
        trailing = "\n" if current and not current.endswith("\n") else ""
        new_text = current + f"{trailing}{key}={value}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")


# Keys whose values should be masked in output (secrets)
_SECRET_KEYS = frozenset(
    {
        *(definition.env_key for definition in PROVIDER_DEFINITIONS if definition.env_key),
        "NORTH_TELEGRAM_BOT_TOKEN",
        "NORTH_SECRET",
        "NORTH_ANTHROPIC_API_KEY",
        "NORTH_OPENAI_API_KEY",
    }
)

# Keys that affect the live inference router and need a rebuild on change.
_INFERENCE_KEYS = frozenset(definition.env_key for definition in PROVIDER_DEFINITIONS if definition.env_key)


_KEY_PREFIX = "NORTH_"
# Shorthand a user is likely to type for a key north stores under its own prefix.
_KEY_ALIASES = {"FAL_KEY": "NORTH_FAL_KEY"}
_SHOWED_CURRENT = " (no value given -> showed current)"


def _dial_output(dial: str, value: str, note: str = "") -> ToolOutput:
    return ToolOutput(success=True, data={"action": dial, "value": value, "note": note})


class NorthConfigTool(Tool):
    """Read and update north's configuration (.env file).

    Actions:
      ``list``     — show all config keys with masked values.
      ``get <key>`` — show a single config value.
      ``set <key>=<value>`` — add or update a config key. Requires approval.

    Keys use the ``NORTH_`` prefix convention. Values are written to the .env
    file and take effect immediately: settings are reloaded from disk and — for
    inference keys — the live router is rebuilt in place, so no restart is
    required.
    """

    name = "north_config"
    is_mutating = True  # 'set' action requires approval
    description = (
        "Read or update north's own configuration. "
        "Use 'list' to show all settings, 'get <key>' to read one, "
        "'set <key>=<value>' to add/update a setting. "
        "Example: set NORTH_OPENCODE_ZEN_API_KEY=abc123 will write it to the "
        ".env file and north starts using that provider immediately."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "set", "power", "autonomy"],
                "description": "Actions: 'list' (show all keys), "
                "'get <key>' (show one), 'set <key>=<value>' (write a key), "
                "'power [eco|cruise|sport]' (get/set model-selection dial), "
                "'autonomy [interactive|auto|autonomous]' (get/set approval dial)",
            },
            "key": {
                "type": "string",
                "description": "Config key name for 'get' or 'set' actions (e.g. NORTH_OPENCODE_ZEN_API_KEY)",
            },
            "value": {
                "type": "string",
                "description": "Value to set for 'set' action",
            },
        },
        "required": ["action"],
    }

    def _env_path(self) -> Path:
        """Path to the .env file."""
        home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
        return home / ".env"

    def _settings_path(self) -> Path:
        """Path to the NorthSettings JSON file."""
        home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
        return home / "settings.json"

    def _read_env(self) -> dict[str, str]:
        """Parse the .env file into a dict. Returns empty dict on any error."""
        path = self._env_path()
        if not path.exists():
            return {}
        env: dict[str, str] = {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(NORTH_[A-Z_]+)=(.*)", line)
            if m:
                env[m.group(1)] = m.group(2)
        return env

    def _mask(self, key: str, value: str) -> str:
        """Mask secret values for display."""
        if key in _SECRET_KEYS and value:
            return value[:6] + "***" + value[-4:] if len(value) > 12 else value[:3] + "***"
        return value

    def _apply_runtime(self, key: str) -> str:
        """Reload settings and rebuild the live router if the key is inference-related.

        Returns a human-readable note describing what took effect.
        """
        from config.runtime import get_runtime
        from config.settings import reload_settings
        from inference.factory import build_router

        reload_settings()
        if key in _INFERENCE_KEYS:
            deps = get_runtime()
            if deps is None:
                return "\n⚠️ north not running as a server — change will apply on next restart."
            from config.settings import settings

            new_router = build_router(
                openrouter_api_key=settings.openrouter_api_key,
                north_settings=deps.north_settings,
                groq_api_key=settings.groq_api_key,
                gemini_api_key=settings.gemini_api_key,
                opencode_zen_api_key=settings.opencode_zen_api_key,
                provider_settings=settings,
                confidence_tracker=deps.confidence_tracker,
                cooldowns_path=settings.north_home / "cooldowns.json",
            )
            # Swap the wrapped router inside the live CostTracker in place.
            deps.cost_tracker.set_inner(new_router)
            providers = ", ".join(p.name for p in new_router._providers)  # type: ignore[attr-defined]
            return f"\n✅ Inference reloaded — providers: {providers}"
        return "\n✅ Settings reloaded from disk."

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action", "")
        if action == "list":
            items = data.get("entries", [])
            if not items:
                return "No config entries found."
            lines = sorted(f"  {k}={self._mask(k, v)}" for k, v in items)
            env_path = self._env_path()
            return f"Config ({env_path}):\n" + "\n".join(lines)
        elif action == "get":
            key = data.get("key", "")
            value = data.get("value", "")
            if value is None:
                return f"Key `{key}` is not set."
            return f"`{key}` = `{self._mask(key, value)}`"
        elif action == "set":
            key = data.get("key", "")
            value = data.get("value", "")
            note = data.get("note", "")
            return f"✅ Written `{key}={self._mask(key, value)}` to `{self._env_path()}`.{note}"
        elif action in ("power", "autonomy"):
            value = data.get("value", "")
            note = data.get("note", "")
            label = "power (model-selection)" if action == "power" else "autonomy (approval)"
            return f"⚙️ {label} = `{value}`{note}"
        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action", "").strip().lower()
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required (list/get/set).")
        handler = _ACTIONS.get(action)
        if handler is None:
            return ToolOutput(
                success=False,
                error=f"Unknown action: {action!r}. Valid: {', '.join(_ACTIONS)}.",
            )
        return await handler(self, input.params)

    async def _list(self, params: dict) -> ToolOutput:
        return ToolOutput(success=True, data={"action": "list", "entries": sorted(self._read_env().items())})

    async def _get(self, params: dict) -> ToolOutput:
        key = (params.get("key") or "").strip().upper()
        if not key:
            return ToolOutput(success=False, error="Usage: get <KEY> — provide the key name after 'get'.")
        return ToolOutput(success=True, data={"action": "get", "key": key, "value": self._read_env().get(key)})

    async def _set(self, params: dict) -> ToolOutput:
        key = (params.get("key") or "").strip().upper()
        value = (params.get("value") or "").strip()
        if not key or not value:
            return ToolOutput(
                success=False,
                error="Usage: set key=NORTH_FAL_KEY value=xxx — both 'key' and 'value' parameters required.",
            )
        key = _KEY_ALIASES.get(key, key)
        if not key.startswith(_KEY_PREFIX):
            return ToolOutput(success=False, error=f"Config keys must start with {_KEY_PREFIX}. Got: {key!r}")

        # Upsert the key in .env off-thread (CODING_STYLE §10.3).
        await asyncio.to_thread(_upsert_env_key, self._env_path(), key, value)
        return ToolOutput(
            success=True,
            data={"action": "set", "key": key, "value": value, "note": self._apply_runtime(key)},
        )

    async def _power(self, params: dict) -> ToolOutput:
        from config.strategy import NorthSettings, StrategyMode

        north_settings = NorthSettings(self._settings_path())
        requested = (params.get("value") or "").strip().lower()
        if not requested:
            return _dial_output("power", north_settings.power.value, note=_SHOWED_CURRENT)
        try:
            mode = StrategyMode(requested)
        except ValueError:
            return ToolOutput(success=False, error=f"Unknown power mode {requested!r}. Valid: eco, cruise, sport.")
        north_settings.set_power(mode)
        return _dial_output("power", mode.value)

    async def _autonomy(self, params: dict) -> ToolOutput:
        from approval.mode import parse_approval_mode
        from config.strategy import NorthSettings

        north_settings = NorthSettings(self._settings_path())
        requested = (params.get("value") or "").strip().lower()
        if not requested:
            return _dial_output("autonomy", north_settings.autonomy.value, note=_SHOWED_CURRENT)
        mode = parse_approval_mode(requested)
        if mode is None:
            return ToolOutput(
                success=False,
                error=f"Unknown autonomy mode {requested!r}. Valid: interactive, auto, autonomous.",
            )
        north_settings.set_autonomy(mode)
        return _dial_output("autonomy", mode.value)


# Every settings action north_config answers, mapped to the handler that runs it.
_ACTIONS: dict[str, Callable[[NorthConfigTool, dict], Awaitable[ToolOutput]]] = {
    "list": NorthConfigTool._list,
    "get": NorthConfigTool._get,
    "set": NorthConfigTool._set,
    "power": NorthConfigTool._power,
    "autonomy": NorthConfigTool._autonomy,
}
