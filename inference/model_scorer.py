"""ModelScorer - price-free quality scoring for candidate ranking.

Replaces the old price-derived ``base_quality`` as the primary ranking signal.
When every provider is free, price is a flat 0 and carries no information about
model capability; ranking on it degenerates to a near-random shuffle. This scorer
blends three price-free signals instead:

    score = w_fam * family_tier + w_ema * live_ema + w_cur * curation_boost

- ``family_tier``     : static prior from a small family table (opus=0.96 .. 8b=0.3).
                        Unknown families fall back to ``unknown_family_quality``.
- ``live_ema``        : existing per-model success-rate EMA (confidence_tracker).
                        This is the *discovery* signal - models that actually work
                        rise, broken ones sink. Reused from the dispatcher.
- ``curation_boost``  : 1.0 if the model matches the user's preferred specs, else 0.0.

Both the weights (``ScoringConfig``) and the family-tier table are user-editable
and live-reloadable - see ``config/strategy.py`` and ``reload()`` below.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from config.strategy import StrategyMode
from inference.constants import _FREE_MODEL_QUALITY, MODEL_FAMILY_TIERS

logger = logging.getLogger(__name__)

# Built-in family tiers; used as default for ScoringConfig.family_tiers.
# User overrides live in settings.json (ScoringConfig.family_tiers).
_DEFAULT_TIERS = dict(MODEL_FAMILY_TIERS)

# Legacy file path for one-time migration
_LEGACY_TIERS_PATH = Path.home() / ".north" / "model_tiers.json"


@dataclass
class ScoringConfig:
    """User-tunable weights for the model quality score.

    Weights are normalised at scoring time so they need not sum to 1. Edit them
    live via ``north config`` (settings.json ``scoring`` key) - no restart needed.
    """

    family_weight: float = 0.5
    ema_weight: float = 0.3
    curation_weight: float = 0.2
    unknown_family_quality: float = _FREE_MODEL_QUALITY
    # Per-family quality overrides (substring -> 0..1). Longest match wins.
    # Merged on top of _DEFAULT_TIERS at scoring time.
    family_tiers: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> ScoringConfig:
        if not isinstance(raw, dict):
            return cls()
        # Start with built-in defaults
        family_tiers = dict(_DEFAULT_TIERS)
        # User-provided family_tiers in settings.json take precedence
        if "family_tiers" in raw and isinstance(raw["family_tiers"], dict):
            for k, v in raw["family_tiers"].items():
                try:
                    family_tiers[str(k).lower()] = float(v)
                except (TypeError, ValueError):
                    logger.warning("Bad family-tier entry %r=%r; skipped", k, v)
        # One-time migration: if user didn't explicitly set family_tiers in settings
        # but legacy file exists, load it (will be persisted to settings.json on next save).
        elif "family_tiers" not in raw and _LEGACY_TIERS_PATH.exists():
            try:
                overrides = json.loads(_LEGACY_TIERS_PATH.read_text(encoding="utf-8"))
                if isinstance(overrides, dict):
                    for k, v in overrides.items():
                        try:
                            family_tiers[str(k).lower()] = float(v)
                        except (TypeError, ValueError):
                            logger.warning("Bad legacy family-tier entry %r=%r; skipped", k, v)
                    logger.info("Migrated family tiers from %s", _LEGACY_TIERS_PATH)
                    # Rename legacy file so we don't re-migrate on next load
                    migrated_path = _LEGACY_TIERS_PATH.with_suffix(".json.migrated")
                    _LEGACY_TIERS_PATH.rename(migrated_path)
                    logger.info("Renamed legacy file to %s", migrated_path)
            except Exception as exc:
                logger.warning("Could not read legacy %s: %s", _LEGACY_TIERS_PATH, exc)
        return cls(
            family_weight=float(raw.get("family_weight", cls.family_weight)),
            ema_weight=float(raw.get("ema_weight", cls.ema_weight)),
            curation_weight=float(raw.get("curation_weight", cls.curation_weight)),
            unknown_family_quality=float(
                raw.get("unknown_family_quality", cls.unknown_family_quality)
            ),
            family_tiers=family_tiers,
        )

    def to_dict(self) -> dict[str, float | dict[str, float]]:
        return {
            "family_weight": self.family_weight,
            "ema_weight": self.ema_weight,
            "curation_weight": self.curation_weight,
            "unknown_family_quality": self.unknown_family_quality,
            "family_tiers": self.family_tiers,
        }


class ModelScorer:
    """Computes a 0..1 quality score for a candidate model.

    Constructed once per dispatcher. ``reload()`` re-reads the scoring config
    so edits take effect without a restart. Family tiers now live in
    ScoringConfig.family_tiers (merged with built-in defaults at config load time).
    """

    def __init__(
        self,
        config: ScoringConfig | None = None,
        tiers_path: Path | None = None,
    ) -> None:
        self._config = config or ScoringConfig()
        # tiers_path kept for backward compatibility but no longer used.
        # Family tiers are read from self._config.family_tiers (with built-in defaults).
        self._tiers_path = tiers_path or (Path.home() / ".north" / "model_tiers.json")
        # Pre-merge built-in defaults + config overrides once at init.
        self._tiers: dict[str, float] = dict(_DEFAULT_TIERS)
        self._tiers.update(self._config.family_tiers)
        # Cache family_tier lookups: model_id -> score.  Stable between reload()
        # calls, avoids O(n) substring scan on every _effective_quality() call.
        self._family_cache: dict[str, float] = {}

    # ---- live reloading ----

    def reload(self) -> None:
        """Re-read scoring config family_tiers (merged with built-in defaults)."""
        self._tiers = dict(_DEFAULT_TIERS)
        self._tiers.update(self._config.family_tiers)
        self._family_cache.clear()

    def set_config(self, config: ScoringConfig) -> None:
        self._config = config
        # Immediately update merged tiers so scoring reflects new config.
        self._tiers = dict(_DEFAULT_TIERS)
        self._tiers.update(self._config.family_tiers)
        self._family_cache.clear()

    # ---- scoring ----

    def family_tier(self, model_id: str) -> float:
        """Longest matching substring wins; unknown falls back to config floor.

        Results are cached per model_id (the mapping is stable between reload()
        calls), eliminating repeated O(n) substring scans across the tier table.
        """
        cached = self._family_cache.get(model_id)
        if cached is not None:
            return cached
        lower = model_id.lower()
        best_len, best_score = 0, self._config.unknown_family_quality
        for key, score in self._tiers.items():
            if key in lower and len(key) > best_len:
                best_len, best_score = len(key), score
        self._family_cache[model_id] = best_score
        return best_score

    def score(
        self,
        model_id: str,
        ema_score: float,
        is_preferred: bool,
        power: StrategyMode = StrategyMode.CRUISE,
    ) -> float:
        """Blend the three signals into a 0..1 quality score.

        Power dial nudges the blend: SPORT emphasises capability (family + ema),
        ECO reins it in so cost (used as a tie-break by the dispatcher) can dominate
        cheap choices. CRUISE is the default balanced blend.
        """
        fam = self.family_tier(model_id)
        cur = 1.0 if is_preferred else 0.0

        w_fam = self._config.family_weight
        w_ema = self._config.ema_weight
        w_cur = self._config.curation_weight

        if power == StrategyMode.SPORT:
            w_fam, w_ema, w_cur = w_fam * 1.3, w_ema * 1.2, w_cur
        elif power == StrategyMode.ECO:
            w_fam, w_ema, w_cur = w_fam * 0.7, w_ema, w_cur * 0.8

        total = w_fam + w_ema + w_cur
        if total <= 0:
            return fam  # degenerate config -> fall back to family prior only
        return (w_fam * fam + w_ema * ema_score + w_cur * cur) / total
