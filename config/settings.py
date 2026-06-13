"""System-wide configuration settings loaded from environment or .env.

See docs/CODING_STYLE.md Section 17.
"""

from __future__ import annotations

import logging
import os
import stat as _stat
from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr
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
                "%s was group/world-accessible (mode %s) — permissions tightened to 0600.",
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

    # Optional direct-provider keys — enables dedicated rate-limit buckets and
    # lower latency for those providers' models. Empty = provider not used.
    groq_api_key: str = ""
    gemini_api_key: str = ""

    # Paths — NORTH_HOME env var is the canonical override (used in Docker)
    north_home: Path = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()

    # Default workspace for filesystem/shell tools when no workspace is provided per-request.
    # Set via NORTH_NORTH_WORKSPACE env var. Must never default to $HOME — the workspace
    # scopes what tools may touch, and even an explicit broad root cannot re-open the
    # sensitive-path blocklist (~/.ssh, ~/.north, /etc, ...; see tools/_path.py).
    north_workspace: str = ""

    # Pre-shared secret override — set NORTH_SECRET in Docker instead of using a key file
    north_secret: str = os.environ.get("NORTH_SECRET", "")

    # Base URL for the main orchestrator server — override in Docker/multi-host deployments.
    north_orchestrator_url: str = "http://127.0.0.1:8000"

    # Runtime environment
    north_env: Literal["development", "production", "test"] = "development"

    # Tuning parameters
    job_poll_interval_seconds: int = 5
    agent_read_timeout_seconds: int = 30
    task_cleanup_completed_days: int = 7
    task_cleanup_failed_days: int = 30
    confidence_increase_per_helpful_use: float = 0.05
    confidence_decrease_per_unhelpful_use: float = 0.03
    confidence_auto_approve_threshold: float = 0.8
    inference_pool_refresh_interval_hours: int = 6
    agent_max_iterations: int = 40
    agent_history_keep_recent: int = 10

    # Extraction pipeline tuning
    extraction_poll_interval_seconds: int = 120
    extraction_max_daily_cost_usd: float = 0.10
    extraction_min_output_chars: int = 100
    extraction_max_concurrent: int = 5

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


settings = Settings()
