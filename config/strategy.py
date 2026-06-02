"""User-configurable inference strategy. Controls model selection order."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path


class StrategyMode(StrEnum):
    ECO = "eco"        # cheapest model first — maximise cost savings
    CRUISE = "cruise"  # role-aware best fit (default)
    SPORT = "sport"    # most capable model first — maximise quality


_DESCRIPTIONS = {
    StrategyMode.ECO: "Cheapest model first. Saves cost; quality may vary on hard tasks.",
    StrategyMode.CRUISE: "Best fit per task. Balances cost and quality automatically.",
    StrategyMode.SPORT: "Most capable model first. Best quality; higher cost.",
}


def describe(mode: StrategyMode) -> str:
    return _DESCRIPTIONS[mode]


class NorthSettings:
    """Persistent user settings stored at ~/.north/settings.json."""

    _DEFAULT = StrategyMode.CRUISE

    def __init__(self, path: Path) -> None:
        self._path = path
        self._strategy: StrategyMode = self._DEFAULT
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._strategy = StrategyMode(data.get("strategy", self._DEFAULT.value))
        except Exception:
            pass

    @property
    def strategy(self) -> StrategyMode:
        return self._strategy

    def set_strategy(self, mode: StrategyMode) -> None:
        self._strategy = mode
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"strategy": self._strategy.value}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
