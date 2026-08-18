"""Manage north's own configuration — read and update .env settings.

Agents can list config keys, get a specific value, or set (add/update) a
key-value pair. Write operations go through the approval gate so the user
sees exactly what key/value is being changed.

This is how north self-configures: "add a provider key" → agent calls
north_config set NORTH_OPENCODE_ZEN_API_KEY=xxx, and the change takes effect
immediately (the inference router is rebuilt in place — no restart needed).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

# Keys whose values should be masked in output (secrets)
_SECRET_KEYS = frozenset({
    "NORTH_OPENROUTER_API_KEY",
    "NORTH_GROQ_API_KEY",
    "NORTH_GEMINI_API_KEY",
    "NORTH_OPENCODE_ZEN_API_KEY",
    "NORTH_TELEGRAM_BOT_TOKEN",
    "NORTH_SECRET",
    "NORTH_ANTHROPIC_API_KEY",
    "NORTH_OPENAI_API_KEY",
})

# Keys that affect the live inference router and need a rebuild on change.
_INFERENCE_KEYS = frozenset({
    "NORTH_OPENROUTER_API_KEY",
    "NORTH_GROQ_API_KEY",
    "NORTH_GEMINI_API_KEY",
    "NORTH_OPENCODE_ZEN_API_KEY",
})


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
                "enum": ["list", "get", "set", "scoring", "power", "autonomy"],
                "description": "Actions: 'list' (show all keys), "
                "'get <key>' (show one), 'set <key>=<value>' (write a key), "
                "'scoring' (get/set model-quality scoring weights live), "
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
            "family_weight": {
                "type": "number",
                "description": "scoring action: weight for the static family-tier prior (0..1+)",
            },
            "ema_weight": {
                "type": "number",
                "description": "scoring action: weight for the live per-model success EMA (0..1+)",
            },
            "curation_weight": {
                "type": "number",
                "description": "scoring action: weight for the curated preferred-model boost (0..1+)",
            },
            "family_tiers": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                            "description": (
                                "scoring action: per-family quality overrides (substring -> 0..1). "
                                "E.g. {\"opus\": 0.97, \"custom-model\": 0.85}"
                            ),
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
            return f"✅ Written `{key}={self._mask(key, value)}` to " \
                   f"`{self._env_path()}`.{note}"
        elif action == "scoring":
            cfg = data.get("config", {})
            note = data.get("note", "")
            lines = [
                "Model scoring weights:",
                f"  family_weight={cfg.get('family_weight')}",
                f"  ema_weight={cfg.get('ema_weight')}",
                f"  curation_weight={cfg.get('curation_weight')}",
                f"  unknown_family_quality={cfg.get('unknown_family_quality')}",
            ]
            if "family_tiers" in cfg and cfg["family_tiers"]:
                lines.append("  family_tiers:")
                for k, v in sorted(cfg["family_tiers"].items()):
                    lines.append(f"    {k}={v}")
            return "\n".join(lines) + f"{note}"
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

        # ── list ────────────────────────────────────────────────────────────
        if action == "list":
            env = self._read_env()
            if not env:
                return ToolOutput(
                    success=True,
                    data={"action": "list", "entries": []},
                )
            return ToolOutput(
                success=True,
                data={"action": "list", "entries": sorted(env.items())},
            )

        # ── get ─────────────────────────────────────────────────────────────
        if action == "get":
            key = (input.params.get("key") or "").strip().upper()
            if not key:
                return ToolOutput(
                    success=False,
                    error="Usage: get <KEY> — provide the key name after 'get'.",
                )
            env = self._read_env()
            value = env.get(key, None)
            return ToolOutput(
                success=True,
                data={"action": "get", "key": key, "value": value},
            )

        # ── set ─────────────────────────────────────────────────────────────
        if action == "set":
            key = (input.params.get("key") or "").strip().upper()
            value = (input.params.get("value") or "").strip()
            if not key or not value:
                return ToolOutput(
                    success=False,
                    error="Usage: set key=NORTH_FAL_KEY value=xxx — both 'key' and 'value' parameters required.",
                )

            # Normalize shorthand keys
            if key == "FAL_KEY":
                key = "NORTH_FAL_KEY"

            if not key.startswith("NORTH_"):
                return ToolOutput(
                    success=False,
                    error=f"Config keys must start with NORTH_. Got: {key!r}",
                )

            if not value:
                return ToolOutput(
                    success=False,
                    error="Value cannot be empty.",
                )

            # Read current .env content
            path = self._env_path()
            current = ""
            if path.exists():
                current = path.read_text(encoding="utf-8")

            # Replace existing key or append
            pattern = re.compile(rf"^{re.escape(key)}=.*", re.MULTILINE)
            if pattern.search(current):
                new_text = pattern.sub(f"{key}={value}", current)
            else:
                trailing = "\n" if current and not current.endswith("\n") else ""
                new_text = current + f"{trailing}{key}={value}\n"

            # Write
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")

            note = self._apply_runtime(key)

            return ToolOutput(
                success=True,
                data={"action": "set", "key": key, "value": value, "note": note},
            )

        # ── scoring ─────────────────────────────────────────────────────────
        if action == "scoring":
            from config.runtime import get_runtime
            from config.settings import reload_settings
            from config.strategy import NorthSettings
            from inference.model_scorer import ScoringConfig

            # Use NorthSettings (which has the scoring property) not config.settings
            ns = NorthSettings(self._settings_path())
            reload_settings()  # still reload .env for any API key changes
            if any(
                k in input.params
                for k in ("family_weight", "ema_weight", "curation_weight", "family_tiers")
            ):
                cur = ns.scoring
                new = ScoringConfig(
                    family_weight=float(input.params.get("family_weight", cur.family_weight)),
                    ema_weight=float(input.params.get("ema_weight", cur.ema_weight)),
                    curation_weight=float(input.params.get("curation_weight", cur.curation_weight)),
                    unknown_family_quality=cur.unknown_family_quality,
                    family_tiers=dict(input.params.get("family_tiers", cur.family_tiers)),
                )
                ns.set_scoring(new)
                # Push live to the running router (no restart).
                deps = get_runtime()
                applied = False
                if deps is not None:
                    inner = deps.cost_tracker.get_inner()
                    if inner is not None and hasattr(inner, "reload_scoring"):
                        inner.reload_scoring()
                        applied = True
                note = "" if applied else "\n⚠️ north not running as a server — applies on next restart."
                return ToolOutput(
                    success=True,
                    data={"action": "scoring", "config": new.to_dict(), "note": note},
                )
            # Getter
            cfg = ns.scoring.to_dict()
            return ToolOutput(success=True, data={"action": "scoring", "config": cfg})

        # ── power ──────────────────────────────────────────────────────────
        if action == "power":
            from config.strategy import NorthSettings, StrategyMode
            ns = NorthSettings(self._settings_path())
            if "value" in input.params and input.params["value"]:
                val = (input.params["value"] or "").strip().lower()
                try:
                    mode = StrategyMode(val)
                except ValueError:
                    return ToolOutput(
                        success=False,
                        error=f"Unknown power mode {val!r}. Valid: eco, cruise, sport.",
                    )
                ns.set_power(mode)
                note = ""
            else:
                mode = ns.power
                note = " (no value given -> showed current)"
            return ToolOutput(
                success=True,
                data={"action": "power", "value": mode.value, "note": note},
            )

        # ── autonomy ─────────────────────────────────────────────────────────
        if action == "autonomy":
            from approval.mode import parse_approval_mode
            from config.strategy import NorthSettings
            ns = NorthSettings(self._settings_path())
            if "value" in input.params and input.params["value"]:
                val = (input.params["value"] or "").strip().lower()
                mode = parse_approval_mode(val)
                if mode is None:
                    return ToolOutput(
                        success=False,
                        error=f"Unknown autonomy mode {val!r}. Valid: interactive, auto, autonomous.",
                    )
                ns.set_autonomy(mode)
                note = ""
            else:
                mode = ns.autonomy
                note = " (no value given -> showed current)"
            return ToolOutput(
                success=True,
                data={"action": "autonomy", "value": mode.value, "note": note},
            )

        return ToolOutput(
            success=False,
            error=f"Unknown action: {action!r}. Valid: list, get, set, scoring, power, autonomy.",
        )
