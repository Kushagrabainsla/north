"""Constants for the north CLI and TUI — data only, no behaviour.

Kept in one place so commands (`cli/main.py`), the Textual UI (`cli/tui.py`), and
the shared helpers (`cli/formatting.py`, `cli/_client.py`, `cli/_server.py`) all
reference a single source of truth rather than redefining literals inline (§5, §9.6).
"""

from __future__ import annotations

import re
from typing import TypedDict

# ── HTTP client ─────────────────────────────────────────────────────────────
_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT = 30.0


# ── First-run provider setup ────────────────────────────────────────────────
class _Provider(TypedDict):
    name: str
    env_key: str
    description: str
    url: str


_PROVIDERS: list[_Provider] = [
    {
        "name": "OpenRouter",
        "env_key": "NORTH_OPENROUTER_API_KEY",
        "description": "All models — Claude, GPT-4, Gemini, Llama, and more (recommended)",
        "url": "https://openrouter.ai/keys",
    },
    {
        "name": "Groq",
        "env_key": "NORTH_GROQ_API_KEY",
        "description": "Ultra-fast open-source models — Llama, Mixtral",
        "url": "https://console.groq.com/keys",
    },
    {
        "name": "Gemini",
        "env_key": "NORTH_GEMINI_API_KEY",
        "description": "Google Gemini 1.5 Pro and Flash",
        "url": "https://aistudio.google.com/apikey",
    },
]


# ── Pipeline step rendering (task progress table) ───────────────────────────
_STEP_ICONS: dict[str, str] = {
    "classifying": "→",
    "classified": "✓",
    "classified_as_trivial": "✓",
    "north_star_checking": "→",
    "north_star_aligned": "✓",
    "north_star_conflict": "◆",
    "routing": "→",
    "routed": "✓",
    "executing": "→",
    "agent_started": "→",
    "agent_completed": "✓",
    "tool_called": "→",
    "tool_result": "✓",
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
}


# ── Context documents + config keys ─────────────────────────────────────────
_VALID_DOCS = ["public", "private", "privacy_rules", "judgement_rules", "north_stars"]

_CONFIG_KEYS = {
    "ledger.retention_days": ("task_cleanup_completed_days", int),
    "jobs.poll_interval_seconds": ("job_poll_interval_seconds", int),
    "agent.read_timeout_seconds": ("agent_read_timeout_seconds", int),
    "inference.pool_refresh_hours": ("inference_pool_refresh_interval_hours", int),
}


# ── TUI ─────────────────────────────────────────────────────────────────────
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

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
    "/clear": "clear the conversation log",
    "/cost": "show session tokens and cost",
    "/agents": "list registered agents",
    "/strategy": "show the current strategy",
    "/quit": "exit north",
}

# Matches well-formed Textual console-markup spans ([tag] / [/tag]) for stripping.
_MARKUP_RE = re.compile(r"\[/?[^\[\]]*\]")
