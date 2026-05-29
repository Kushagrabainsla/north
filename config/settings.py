"""System-wide configuration settings loaded from environment or .env.

See docs/CODING_STYLE.md Section 17.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from the environment with prefix `NORTH_` or a `.env` file."""

    # Required for production; empty default allows import/initialization without crash
    openrouter_api_key: str = ""

    # Paths — NORTH_HOME env var is the canonical override (used in Docker)
    north_home: Path = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()

    # Default workspace for filesystem/shell tools when no workspace is provided per-request.
    # Set via NORTH_NORTH_WORKSPACE env var. In Docker, defaults to $HOME via docker-compose.
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

    @property
    def secret(self) -> str:
        """Return the shared secret: env var takes priority over the key file."""
        if self.north_secret:
            return self.north_secret
        secret_file = self.north_home / "secret.key"
        if not secret_file.exists():
            return ""
        return secret_file.read_text(encoding="utf-8").strip()

    @property
    def is_development(self) -> bool:
        return self.north_env == "development"

    @property
    def is_test(self) -> bool:
        return self.north_env == "test"

    model_config = {
        "env_file": [str(Path.home() / ".north" / ".env"), ".env"],
        "env_prefix": "NORTH_",
        "extra": "ignore",
    }


settings = Settings()
