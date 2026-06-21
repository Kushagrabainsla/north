"""User-configurable inference strategy. Controls model selection order."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class StrategyMode(StrEnum):
    ECO = "eco"  # cheapest model first - maximise cost savings
    CRUISE = "cruise"  # role-aware best fit (default)
    SPORT = "sport"  # most capable model first - maximise quality


_DESCRIPTIONS = {
    StrategyMode.ECO: "Cheapest model first. Saves cost; quality may vary on hard tasks.",
    StrategyMode.CRUISE: "Best fit per task. Balances cost and quality automatically.",
    StrategyMode.SPORT: "Most capable model first. Best quality; higher cost.",
}


def describe(mode: StrategyMode) -> str:
    return _DESCRIPTIONS[mode]


class NorthSettings:
    """Persistent user settings stored at ~/.north/settings.json."""

    _DEFAULT_STRATEGY = StrategyMode.CRUISE
    _DEFAULT_APPROVAL_TIMEOUT = 300.0

    def __init__(self, path: Path) -> None:
        self._path = path
        self._strategy: StrategyMode = self._DEFAULT_STRATEGY
        self._approval_timeout_seconds: float = self._DEFAULT_APPROVAL_TIMEOUT
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._strategy = StrategyMode(data.get("strategy", self._DEFAULT_STRATEGY.value))
            self._approval_timeout_seconds = float(data.get("approval_timeout_seconds", self._DEFAULT_APPROVAL_TIMEOUT))
        except Exception as exc:
            logger.warning(
                "settings.json is unreadable - resetting to defaults (%s): %s",
                self._path,
                exc,
            )

    @property
    def strategy(self) -> StrategyMode:
        return self._strategy

    @property
    def approval_timeout_seconds(self) -> float:
        return self._approval_timeout_seconds

    def set_strategy(self, mode: StrategyMode) -> None:
        self._strategy = mode
        self._save()

    def set_approval_timeout(self, seconds: float) -> None:
        self._approval_timeout_seconds = max(10.0, seconds)
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "strategy": self._strategy.value,
                        "approval_timeout_seconds": self._approval_timeout_seconds,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to persist strategy settings to %s: %s", self._path, exc)
