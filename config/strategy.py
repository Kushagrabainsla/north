"""User-configurable inference strategy. Controls model selection order."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from approval.mode import ApprovalMode
    from inference.model_scorer import ScoringConfig

logger = logging.getLogger(__name__)


class StrategyMode(StrEnum):
    ECO = "eco"  # cheapest model first - maximise cost savings
    CRUISE = "cruise"  # role-aware best fit (default)
    SPORT = "sport"  # most capable model first - maximise quality


# `power` is the user-facing name for the model-selection dial.
PowerMode = StrategyMode


_DESCRIPTIONS = {
    StrategyMode.ECO: "Cheapest model first. Saves cost; quality may vary on hard tasks.",
    StrategyMode.CRUISE: "Best fit per task. Balances cost and quality automatically.",
    StrategyMode.SPORT: "Most capable model first. Best quality; higher cost.",
}


def describe(mode: StrategyMode) -> str:
    return _DESCRIPTIONS[mode]


def _coerce_preferred(raw: object) -> dict[str, list[str]]:
    """Coerce a settings.json value into a ``{pool: [specs]}`` map, dropping junk.

    Kept dependency-free (no inference import) so the config layer stays
    independent; the wiring layer parses env/defaults via
    ``inference.model_policy.parse_preferred``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for pool, val in raw.items():
        if not isinstance(pool, str) or not isinstance(val, list):
            continue
        specs = [str(s).strip() for s in val if str(s).strip()]
        if specs:
            out[pool] = specs
    return out


class NorthSettings:
    """Persistent user settings stored at ~/.north/settings.json."""

    _DEFAULT_POWER = StrategyMode.CRUISE
    _DEFAULT_APPROVAL_TIMEOUT = 300.0

    def __init__(self, path: Path, default_approval_mode: ApprovalMode | None = None,
                 default_preferred_models: dict[str, list[str]] | None = None) -> None:
        from approval.mode import ApprovalMode

        self._path = path
        self._power: StrategyMode = self._DEFAULT_POWER
        self._approval_timeout_seconds: float = self._DEFAULT_APPROVAL_TIMEOUT
        # Startup default (e.g. from NORTH_APPROVAL_MODE); settings.json overrides it.
        self._autonomy: ApprovalMode = default_approval_mode or ApprovalMode.INTERACTIVE
        # Curated preferred models per pool (see inference/model_policy.py). The
        # startup default comes from env/DEFAULT_PREFERRED_MODELS via the wiring
        # layer; a "preferred_models" key in settings.json overrides it live.
        # `_preferred_explicit` tracks whether the value was *deliberately* chosen
        # (loaded from a file that had the key, or set via set_preferred_models) so
        # a routine _save (e.g. changing power) never freezes the built-in
        # default into settings.json - which would otherwise stop future default
        # improvements from ever reaching the user.
        self._preferred_models: dict[str, list[str]] = dict(default_preferred_models or {})
        self._preferred_explicit: bool = False
        # Model-quality scoring weights (see inference/model_scorer.py). The
        # startup default comes from ScoringConfig(); a "scoring" key in
        # settings.json overrides it live (reloadable, no restart needed).
        from inference.model_scorer import ScoringConfig

        self._scoring: ScoringConfig = ScoringConfig()
        # Per-part routing overrides (see inference/routing/parts.py). Profiles are
        # data, so an install can retune which part gets which model without a code
        # change. Persisted only when deliberately set, like scoring above.
        self._routing_parts: dict[str, object] = {}
        self._load()

    def _load(self) -> None:
        from approval.mode import parse_approval_mode

        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_power = data.get("power")
            self._power = StrategyMode(raw_power or self._DEFAULT_POWER.value)
            self._approval_timeout_seconds = float(data.get("approval_timeout_seconds", self._DEFAULT_APPROVAL_TIMEOUT))
            raw_autonomy = data.get("autonomy")
            self._autonomy = parse_approval_mode(raw_autonomy) or self._autonomy
            if "preferred_models" in data:
                self._preferred_models = _coerce_preferred(data.get("preferred_models"))
                self._preferred_explicit = True
            if "scoring" in data:
                from inference.model_scorer import ScoringConfig

                self._scoring = ScoringConfig.from_dict(data.get("scoring"))
            routing = data.get("routing")
            if isinstance(routing, dict) and isinstance(routing.get("parts"), dict):
                self._routing_parts = routing["parts"]
        except Exception as exc:
            logger.warning(
                "settings.json is unreadable - resetting to defaults (%s): %s",
                self._path,
                exc,
            )

    @property
    def power(self) -> StrategyMode:
        return self._power

    @property
    def approval_timeout_seconds(self) -> float:
        return self._approval_timeout_seconds

    @property
    def autonomy(self) -> ApprovalMode:
        return self._autonomy

    @property
    def preferred_models(self) -> dict[str, list[str]]:
        """Curated preferred models per pool. Empty pools fall back to price ranking."""
        return self._preferred_models

    @property
    def scoring(self) -> ScoringConfig:
        """Model-quality scoring weights. Live-reloadable via set_scoring()."""
        return self._scoring

    @property
    def routing_parts(self) -> dict[str, object]:
        """Raw per-part routing overrides; parsed by inference.routing.parts."""
        return self._routing_parts

    def set_routing_parts(self, parts: dict[str, object]) -> None:
        self._routing_parts = parts if isinstance(parts, dict) else {}
        self._save()

    def set_scoring(self, config: ScoringConfig) -> None:
        self._scoring = config
        self._save()

    def set_power(self, mode: StrategyMode) -> None:
        self._power = mode
        self._save()

    def set_preferred_models(self, mapping: dict[str, list[str]]) -> None:
        self._preferred_models = _coerce_preferred(mapping)
        self._preferred_explicit = True
        self._save()

    def set_approval_timeout(self, seconds: float) -> None:
        self._approval_timeout_seconds = max(10.0, seconds)
        self._save()

    def set_autonomy(self, mode: ApprovalMode) -> None:
        self._autonomy = mode
        self._save()

    def _save(self) -> None:
        try:
            from inference.model_scorer import ScoringConfig

            self._path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, object] = {
                "power": self._power.value,
                "approval_timeout_seconds": self._approval_timeout_seconds,
                "autonomy": self._autonomy.value,
            }
            # Only persist preferred_models when the user deliberately chose it, so
            # a routine save never freezes the built-in default and block future
            # default improvements from reaching this install.
            if self._preferred_explicit:
                data["preferred_models"] = self._preferred_models
            if self._routing_parts:
                data["routing"] = {"parts": self._routing_parts}
            # Persist scoring only when the user overrode the default weights, so a
            # routine save doesn't freeze the built-in defaults.
            if data.get("scoring") is not None or self._scoring != ScoringConfig():
                if self._scoring != ScoringConfig():
                    data["scoring"] = self._scoring.to_dict()
                elif "scoring" in data:
                    # user reset to defaults - drop the key
                    del data["scoring"]
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist settings to %s: %s", self._path, exc)
