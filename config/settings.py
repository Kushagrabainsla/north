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

from pydantic import Field, PrivateAttr
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

    # Runtime environment
    north_env: Literal["development", "production", "test"] = "development"

    # Tuning parameters
    job_poll_interval_seconds: int = Field(default=5, ge=1)
    agent_read_timeout_seconds: int = Field(default=30, ge=1)
    task_cleanup_completed_days: int = Field(default=7, ge=0)
    task_cleanup_failed_days: int = Field(default=30, ge=0)
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

    # Run mutating agents (see orchestrator.constants.WORKTREE_ISOLATION_AGENTS) in a
    # dedicated git worktree when the workspace is a git repo, applying changes back
    # on success. Off by default; opt in with NORTH_WORKTREE_ISOLATION_ENABLED=1.
    worktree_isolation_enabled: bool = False

    # Where isolated worktrees are created. Empty = a "north-worktrees" dir under the
    # system temp dir. Never place this inside a workspace or under ~/.north.
    worktree_root: str = ""

    # Best-of-N (#11): run this many independent coder attempts in parallel isolated
    # worktrees and integrate only the best one. 1 = off (a single attempt, no change
    # in behaviour). Requires worktree isolation + a git repo. Costs N× the coder's
    # inference, so raise deliberately (e.g. 2-3) for high-stakes changes.
    best_of_n: int = 1

    # Optional shell command used to score each best-of-N candidate (exit 0 = pass).
    # Run inside each candidate's worktree. Empty = score by diff size only.
    best_of_n_test_command: str = ""

    # Optional shell command the orchestrator runs itself after a conductor coding
    # task, as an independent executable oracle feeding the Definition-of-Done (exit
    # 0 = pass). Run once in the task workspace. Empty = auto-detect a safe, fixed
    # test command (pytest -q / go test ./... / cargo test) from the project, or skip
    # if none is detected. A failed run fails the DoD; a missing/errored run never does.
    verify_command: str = ""

    # Sandboxed execution (#6): run bash-tool commands inside a Docker container that
    # only sees the workspace, with the network off and memory/CPU/PID limits. Off by
    # default; when enabled it FAILS CLOSED (refuses to run) if Docker is unavailable.
    sandbox_enabled: bool = False
    sandbox_image: str = "python:3.12-slim"
    sandbox_network_disabled: bool = True
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1"
    sandbox_pids_limit: int = 512

    # Approval mode - one dial for how much north does without asking:
    #   "interactive" (default): read-only runs free, every mutation asks
    #   "auto":       + auto-approve the safe engineering subset (in-workspace edits,
    #                   test/lint/build allowlist, local git); ask for the rest
    #   "autonomous": auto-approve everything except a hard-danger floor
    # Set via NORTH_APPROVAL_MODE. Empty = fall back to the legacy booleans below,
    # then to "interactive". See approval/mode.py.
    approval_mode: str = ""

    # Legacy boolean toggles - still honoured as a fallback when approval_mode is
    # left at its default, so older configs keep working. Prefer approval_mode.
    # unattended -> "auto"; autonomous -> "autonomous".
    unattended_mode: bool = False
    unattended_extra_commands: tuple[str, ...] = ()
    autonomous_mode: bool = False

    # Telegram bot token for the Telegram gateway.
    # Set NORTH_TELEGRAM_BOT_TOKEN in environment or .env.
    telegram_bot_token: str = ""

    # Optional comma-separated list of allowed Telegram chat IDs or user IDs.
    # When set, messages from other chats/users are rejected with an access error.
    # Set NORTH_TELEGRAM_ALLOWED_CHAT_IDS="12345678,87654321" in environment or .env.
    telegram_allowed_chat_ids: str = ""

    # Curated preferred models per pool, as a JSON object string, e.g.
    #   NORTH_PREFERRED_MODELS='{"reasoning": ["anthropic/claude-sonnet", "openai/gpt-4.1"]}'
    # This is the startup default; a "preferred_models" key in ~/.north/settings.json
    # overrides it live. Empty = use the built-in DEFAULT_PREFERRED_MODELS. See
    # inference/model_policy.py. Entries are family-matched against the live catalog.
    preferred_models: str = ""

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
    extraction_poll_interval_seconds: int = Field(default=120, ge=1)
    extraction_max_daily_cost_usd: float = Field(default=0.10, ge=0.0)
    extraction_min_output_chars: int = Field(default=100, ge=0)
    extraction_max_concurrent: int = Field(default=5, ge=1)

    @property
    def parsed_telegram_allowed_chat_ids(self) -> frozenset[int]:
        if not self.telegram_allowed_chat_ids:
            return frozenset()
        ids = set()
        for token in self.telegram_allowed_chat_ids.split(","):
            token = token.strip()
            if token:
                with contextlib.suppress(ValueError):
                    ids.add(int(token))
        return frozenset(ids)

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

    # Only ~/.north/.env is a trusted config source. A .env in the CWD is
    # attacker-influenced in any cloned repo and must never override config
    # (e.g. NORTH_SECRET), so it is deliberately not loaded.
    model_config = {
        "env_file": str(Path.home() / ".north" / ".env"),
        "env_prefix": "NORTH_",
        "extra": "ignore",
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
