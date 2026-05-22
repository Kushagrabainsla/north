"""System-wide configuration settings loaded from environment or .env.

See docs/CODING_STYLE.md Section 17.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from the environment with prefix `NORTH_` or a `.env` file."""

    # Required for production; empty default allows import/initialization without crash
    openrouter_api_key: str = ""

    # Paths
    north_home: Path = Path("~/.north").expanduser()

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

    @property
    def secret(self) -> str:
        """Read the local shared secret from north_home."""
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
        "env_file": ".env",
        "env_prefix": "NORTH_",
        "extra": "ignore",
    }


settings = Settings()
