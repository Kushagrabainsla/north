"""System-wide configuration settings loaded from environment or .env.

See docs/CODING_STYLE.md Section 17.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat as _stat
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, PrivateAttr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def read_secret_file(secret_file: Path) -> str:
    """Read a secret key file, enforcing owner-only permissions (fail closed).

    A group/world-readable key file is tightened to 0600 before the secret is
    used; if that fails the read is refused rather than proceeding with an
    exposed key.
    """
    mode = secret_file.stat().st_mode
    if mode & (_stat.S_IRWXG | _stat.S_IRWXO):
        try:
            secret_file.chmod(0o600)
            logger.warning(
                "%s was group/world-accessible (mode %s) - permissions tightened to 0600.",
                secret_file,
                oct(mode & 0o777),
            )
        except OSError as exc:
            raise PermissionError(
                f"{secret_file} is group/world-accessible (mode {oct(mode & 0o777)}) and could not be "
                f"fixed automatically ({exc}). Run: chmod 600 {secret_file}"
            ) from exc
    return secret_file.read_text(encoding="utf-8").strip()


class Settings(BaseSettings):
    """Configuration loaded from the environment with prefix `NORTH_` or a `.env` file."""

    # In-memory cache for the secret so the key file is only read once.
    _secret_cache: str = PrivateAttr(default="")

    # Required for production; empty default allows import/initialization without crash
    openrouter_api_key: str = ""

    # Optional direct-provider keys - enables dedicated rate-limit buckets and
    # lower latency for those providers' models. Empty = provider not used.
    groq_api_key: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""

    # OpenCode Zen API key for inference.
    # Set NORTH_OPENCODE_ZEN_API_KEY in environment or .env.
    opencode_zen_api_key: str = ""

    # Paths - NORTH_HOME env var is the canonical override (used in Docker)
    north_home: Path = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()

    # Default workspace for filesystem/shell tools when no workspace is provided per-request.
    # Set via NORTH_NORTH_WORKSPACE env var. Must never default to $HOME - the workspace
    # scopes what tools may touch, and even an explicit broad root cannot re-open the
    # sensitive-path blocklist (~/.ssh, ~/.north, /etc, ...; see tools/_path.py).
    north_workspace: str = ""

    # Pre-shared secret override - set NORTH_SECRET in Docker instead of using a key file
    north_secret: str = os.environ.get("NORTH_SECRET", "")

    # Base URL for the main orchestrator server - override in Docker/multi-host deployments.
    north_orchestrator_url: str = "http://127.0.0.1:8000"

    # Runtime environment. NORTH_ENV is the canonical spelling used by Docker
    # and operators; accept the historical doubled form for existing installs.
    north_env: Literal["development", "production", "test"] = Field(
        default="development",
        validation_alias=AliasChoices("NORTH_ENV", "NORTH_NORTH_ENV"),
    )

    # Test mode is inert by default: API/storage wiring is available, but the
    # process does not resume work, fire schedules, contact gateways, or scan
    # personal files. Either capability can still be enabled explicitly for an
    # end-to-end test that needs it. None means "enabled outside test mode".
    autonomous_background_tasks_enabled: bool | None = None
    onboarding_enabled: bool | None = None

    # Tuning parameters
    job_poll_interval_seconds: int = Field(default=5, ge=1)
    agent_read_timeout_seconds: int = Field(default=30, ge=1)
    # Keep the complete execution record long enough for a solo operator to
    # inspect old work. Cleanup preserves each task's final response forever;
    # these windows govern only the detailed execution history around it.
    task_cleanup_completed_days: int = Field(default=365, ge=1)
    task_cleanup_failed_days: int = Field(default=365, ge=1)
    # How long a task's handoff artifacts (research notes, specs, QA reports)
    # stay readable in the cockpit. This matches the detailed ledger window so
    # the evidence behind a session does not disappear before its execution trace.
    # 0 keeps them forever.
    handoff_retention_days: int = Field(default=365, ge=0)
    # Root log level. DEBUG carries the diagnostics that answer questions the
    # normal logs cannot - which prompt tokens a provider served from cache, for
    # one - and those were unreachable while this was hardcoded to INFO.
    log_level: str = Field(default="INFO")
    confidence_increase_per_helpful_use: float = Field(default=0.05, ge=0.0, le=1.0)
    confidence_decrease_per_unhelpful_use: float = Field(default=0.03, ge=0.0, le=1.0)
    confidence_auto_approve_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    inference_pool_refresh_interval_hours: int = Field(default=6, ge=1)
    inference_pool_refresh_interval_seconds: int = Field(default=180, ge=10)
    # Tool-call rounds one agent may take before it is cut off. This is a runaway
    # guard, not a budget (compaction and the cost ledger are the budget), so it is
    # set well above what a real coding task needs - at 40 the coder was hitting it
    # on an ordinary multi-file feature and returning without a final answer.
    agent_max_iterations: int = Field(default=120, ge=1)
    agent_history_keep_recent: int = Field(default=10, ge=1)

    # Port for the local approval-callback server. Separate from the API port so a
    # second instance (or an unrelated process on the default) can be moved aside.
    callback_port: int = Field(default=8001, ge=1, le=65535)
    planner_max_attempts: int = Field(default=3, ge=1)
    planner_retry_delay_seconds: float = Field(default=6.0, ge=0.5)
    planner_retry_backoff_factor: float = Field(default=1.5, ge=1.0)

    # Sandboxed execution (#6): run bash-tool commands inside a Docker container that
    # only sees the workspace, with the network off and memory/CPU/PID limits. Off by
    # default; when enabled it FAILS CLOSED (refuses to run) if Docker is unavailable.
    sandbox_enabled: bool = False
    # OS sandbox for the bash tool (macOS Seatbelt): a command that only reads runs
    # without a card, and an approved one can write only inside its workspace. On
    # by default; ignored when Docker is enabled (one sandbox layer) or unavailable.
    os_sandbox_enabled: bool = True
    sandbox_image: str = "python:3.12-slim"
    sandbox_network_disabled: bool = True
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1"
    sandbox_pids_limit: int = 512

    # Approval mode - one dial for how much north does without asking: ask
    # (default), safe, autonomous or yolo, defined in config/approval_mode.py.
    # Set via NORTH_APPROVAL_MODE. Empty = fall back to the legacy booleans below,
    # then to "ask".
    approval_mode: str = ""

    # Legacy boolean toggles - still honoured as a fallback when approval_mode is
    # left at its default, so older configs keep working. Prefer approval_mode.
    # unattended -> "safe"; autonomous -> "autonomous".
    unattended_mode: bool = False
    unattended_extra_commands: tuple[str, ...] = ()
    autonomous_mode: bool = False

    # Telegram bot token for the Telegram gateway.
    # Set NORTH_TELEGRAM_BOT_TOKEN in environment or .env.
    telegram_bot_token: str = ""

    # Comma-separated numeric Telegram chat IDs or user IDs allowed to use the bot.
    # Required when a bot token is set: the gateway will not start without a
    # valid list, because a bot anyone can find would run tasks for anyone.
    # Set NORTH_TELEGRAM_ALLOWED_CHAT_IDS="12345678,87654321" in environment or .env.
    telegram_allowed_chat_ids: str = ""

    # TP-Link cloud account, for Kasa devices that speak KLAP - which is most
    # firmware shipped since 2023. Discovery finds those devices without any
    # credential and then cannot authenticate to them, so without these the tool
    # can see the bulbs and do nothing with them. Older devices need neither.
    kasa_username: str = ""
    kasa_password: str = ""

    # A task whose heartbeat has not advanced for this long is considered stuck and
    # is cancelled/failed by the watchdog; the same age caps how old an interrupted
    # task may be before startup fails it instead of resuming. Default 24 hours.
    stuck_task_max_age_seconds: int = Field(default=86_400, ge=60)

    # Auto-resume interrupted tasks that already performed a side effect (a mutating
    # tool succeeded). Off by default: re-running could duplicate the action, so such
    # tasks are failed with a note for the user to re-submit deliberately.
    resume_side_effecting_tasks: bool = False

    # When an agent's answer makes a claim with no tool evidence (see
    # orchestrator/verification.py), give the agent one correction pass to either
    # do the work or drop the claim before the answer is flagged. Adds one LLM
    # call only when a violation is detected (rare). Opt out with =0.
    self_repair_enabled: bool = True

    # Duplicate submissions with the same idempotency key (or same source+prompt)
    # within this window collapse to one task - mainly to absorb re-delivered
    # webhooks. 0 disables deduplication.
    idempotency_window_seconds: int = Field(default=60, ge=0)

    # Run a fast LLM "reviewer" over each agent answer to catch answers that do
    # not actually address the request, annotating a note when a gap is found.
    # Off by default - it adds one LLM call per agent result.
    critic_enabled: bool = False

    # Extraction pipeline tuning
    extraction_max_daily_cost_usd: float = Field(default=0.10, ge=0.0)
    extraction_min_output_chars: int = Field(default=100, ge=0)
    extraction_max_concurrent: int = Field(default=5, ge=1)

    @property
    def parsed_telegram_allowed_chat_ids(self) -> frozenset[int]:
        ids = set()
        for token in self._telegram_allowlist_entries():
            with contextlib.suppress(ValueError):
                ids.add(int(token))
        return frozenset(ids)

    @property
    def telegram_allowlist_problem(self) -> str:
        """Why the Telegram allowlist cannot be trusted, or "" when it can.

        An entry that is not a number is not skipped: it is most likely the
        owner's id mistyped (``@name`` instead of the numeric id), and skipping it
        once left the list empty - which let everyone in.
        """
        entries = self._telegram_allowlist_entries()
        if not entries:
            return "NORTH_TELEGRAM_ALLOWED_CHAT_IDS is empty"
        invalid = [entry for entry in entries if not entry.lstrip("-").isdigit()]
        if invalid:
            return f"NORTH_TELEGRAM_ALLOWED_CHAT_IDS has entries that are not numeric ids: {', '.join(invalid)}"
        return ""

    @property
    def telegram_ready(self) -> bool:
        """A bot token and a trustworthy allowlist - the only state the gateway runs in."""
        return bool(self.telegram_bot_token) and not self.telegram_allowlist_problem

    def _telegram_allowlist_entries(self) -> list[str]:
        return [token.strip() for token in self.telegram_allowed_chat_ids.split(",") if token.strip()]

    @property
    def secret(self) -> str:
        """Return the shared secret: env var takes priority over the key file.

        The key-file path is read once and cached in ``_secret_cache`` so that
        subsequent calls (one per authenticated request) do not hit the filesystem.
        """
        if self.north_secret:
            return self.north_secret
        if self._secret_cache:
            return self._secret_cache
        secret_file = self.north_home / "secret.key"
        if not secret_file.exists():
            return ""
        value = read_secret_file(secret_file)
        self._secret_cache = value
        return value

    @property
    def is_development(self) -> bool:
        return self.north_env == "development"

    @property
    def is_test(self) -> bool:
        return self.north_env == "test"

    @property
    def autonomous_background_tasks_active(self) -> bool:
        configured = self.autonomous_background_tasks_enabled
        return not self.is_test if configured is None else configured

    @property
    def onboarding_active(self) -> bool:
        configured = self.onboarding_enabled
        return not self.is_test if configured is None else configured

    # Only ~/.north/.env is a trusted config source. A .env in the CWD is
    # attacker-influenced in any cloned repo and must never override config
    # (e.g. NORTH_SECRET), so it is deliberately not loaded.
    model_config = {
        "env_file": str(Path.home() / ".north" / ".env"),
        "env_prefix": "NORTH_",
        "extra": "ignore",
        "populate_by_name": True,
    }


def reload_settings() -> Settings:
    """Re-read ~/.north/.env and return a fresh Settings instance.

    Import sites that captured the old `settings` singleton (e.g.
    `from config.settings import settings`) keep their reference, so callers
    that need live values must re-import or use this function. The module-level
    `settings` object below is updated in place so existing references stay
    valid without restart.
    """
    fresh = Settings()
    # Update the singleton in place so pre-existing references see new values.
    for field_name in fresh.model_fields:
        setattr(settings, field_name, getattr(fresh, field_name))
    return settings


settings = Settings()
