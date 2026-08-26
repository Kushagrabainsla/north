"""Constants for the north CLI and TUI - data only, no behaviour.

Kept in one place so commands (`cli/main.py`), the Textual UI (`cli/tui.py`), and
the shared helpers (`cli/formatting.py`, `cli/_client.py`, `cli/_server.py`) all
reference a single source of truth rather than redefining literals inline (§5, §9.6).
"""

from __future__ import annotations

import re
from typing import TypedDict

from inference.registry import PROVIDER_DEFINITIONS

# ── HTTP client ─────────────────────────────────────────────────────────────
_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT = 30.0


# ── First-run provider setup ────────────────────────────────────────────────
class _Provider(TypedDict):
    id: str
    name: str
    env_key: str
    auth_kind: str
    description: str
    url: str


_PROVIDERS: list[_Provider] = [
    {
        "id": definition.id,
        "name": definition.display_name,
        "env_key": definition.env_key or "",
        "auth_kind": definition.auth_kind.value,
        "description": definition.description,
        "url": definition.setup_url or "",
    }
    for definition in PROVIDER_DEFINITIONS
]


# ── Design Tokens & Theme ───────────────────────────────────────────────────
THEME = {
    "bg_app": "#090d13",
    "bg_card": "#161b22",
    "bg_subtle": "#21262d",
    "border_subtle": "#30363d",
    "border_focus": "#58a6ff",
    "text_primary": "#f0f6fc",
    "text_secondary": "#8b949e",
    "text_dim": "#484f58",
    "brand": "#a371f7",
    "accent": "#58a6ff",
    "success": "#3fb950",
    "warning": "#d29922",
    "danger": "#f85149",
}


# ── Pipeline step rendering (task progress table) ───────────────────────────
_STEP_ICONS: dict[str, str] = {
    "classifying": "●",
    "classified": "✓",
    "classified_as_trivial": "✓",
    "north_star_checking": "●",
    "north_star_aligned": "✓",
    "north_star_conflict": "▲",
    "routing": "●",
    "routed": "✓",
    "executing": "●",
    "agent_started": "●",
    "agent_completed": "✓",
    "tool_called": "⚙",
    "tool_result": "✓",
    "waiting_for_model": "○",
    "task_queued": "○",
    "task_resumed": "↻",
}

_STEP_LABELS: dict[str, str] = {
    "classifying": "classifying…",
    "classified": "classified",
    "classified_as_trivial": "quick task",
    "north_star_checking": "checking goals…",
    "north_star_aligned": "goals aligned",
    "north_star_conflict": "goal conflict",
    "routing": "planning…",
    "routed": "plan ready",
    "executing": "running agents…",
    "waiting_for_model": "waiting for model capacity…",
    "task_queued": "queued (waiting for models)…",
    "task_resumed": "resuming from queue…",
}



# ── Context documents + config keys ─────────────────────────────────────────
_VALID_DOCS = ["user", "judgement_rules", "north_stars", "soul"]

def _bool_cast(v: str) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


_CONFIG_KEYS = {
    **{
        f"{definition.id}.api_key": (definition.settings_field, str)
        for definition in PROVIDER_DEFINITIONS
        if definition.auth_kind.value == "api_key" and definition.settings_field
    },
    # Telegram integration
    "telegram.bot_token": ("telegram_bot_token", str),
    "telegram.allowed_chat_ids": ("telegram_allowed_chat_ids", str),
    # Approval & Autonomy
    "approval_mode": ("approval_mode", str),
    "unattended_mode": ("unattended_mode", _bool_cast),
    "autonomous_mode": ("autonomous_mode", _bool_cast),
    # Sandboxing & Git
    "sandbox.enabled": ("sandbox_enabled", _bool_cast),
    "worktree.enabled": ("worktree_isolation_enabled", _bool_cast),
    # Tuning
    "ledger.retention_days": ("task_cleanup_completed_days", int),
    "jobs.poll_interval_seconds": ("job_poll_interval_seconds", int),
    "agent.read_timeout_seconds": ("agent_read_timeout_seconds", int),
    "inference.pool_refresh_hours": ("inference_pool_refresh_interval_hours", int),
}


# ── TUI ─────────────────────────────────────────────────────────────────────
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Max chars of the model's private reasoning shown as a dim "thinking" preview.
_REASONING_PREVIEW_CHARS = 120

# Seconds between SSE reconnect attempts; doubles on each failure up to _SSE_BACKOFF_MAX.
_SSE_BACKOFF_BASE = 2.0
_SSE_BACKOFF_MAX = 30.0

# Fill-bar colour thresholds: (max_fill_fraction, hex_colour). The first row
# whose fraction the fill is below wins, so order matters (low → high).
_FILL_COLOURS = (
    (0.50, "#3fb950"),  # green
    (0.75, "#d29922"),  # yellow
    (0.90, "#db6d28"),  # orange
    (1.01, "#f85149"),  # red
)

# Slash commands handled locally by the TUI (never sent to the orchestrator).
_SLASH_COMMANDS: dict[str, str] = {
    "/help": "show available commands",
    "/details": "toggle compact summary vs detailed execution steps (Ctrl+O)",
    "/thoughts": "toggle thoughts inside the current message (Ctrl+T)",
    "/tools": "inspect recent tool calls, arguments, and diffs (Ctrl+I)",
    "/inspect": "alias for /tools (Ctrl+I)",
    "/plan": "inspect execution plan and DoD criteria (Ctrl+P)",
    "/steer": "steer active agent with guidance, e.g. '/steer use asyncpg'",
    "/models": "list discovered models across capability pools",
    "/limits": "inspect active rate limits and cooldowns",
    "/queue": "inspect active tasks and queued background jobs",
    "/cancel": "cancel a running/queued task by ID, or '/cancel all'",
    "/jobs": "inspect active and scheduled background jobs",
    "/context": "inspect active context and goal documents",
    "/clear": "clear the conversation log",
    "/cost": "show session tokens and cost",
    "/agents": "list registered agents",
    "/power": "show or set the model-selection dial (eco|cruise|sport)",
    "/autonomy": "show or set the approval dial (interactive|auto|autonomous)",
    "/quit": "exit north",
}


# Matches well-formed Textual console-markup spans ([tag] / [/tag]) for stripping.
_MARKUP_RE = re.compile(r"\[/?[^\[\]]*\]")
