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
from dataclasses import dataclass
from pathlib import Path

from config.strategy import StrategyMode
from inference.constants import _FREE_MODEL_QUALITY, MODEL_FAMILY_TIERS

logger = logging.getLogger(__name__)

# Built-in family tiers; a per-install ~/.north/model_tiers.json (substring->0..1)
# is merged on top of these at reload time.
_DEFAULT_TIERS = dict(MODEL_FAMILY_TIERS)


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

    @classmethod
    def from_dict(cls, raw: object) -> ScoringConfig:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            family_weight=float(raw.get("family_weight", cls.family_weight)),
            ema_weight=float(raw.get("ema_weight", cls.ema_weight)),
            curation_weight=float(raw.get("curation_weight", cls.curation_weight)),
            unknown_family_quality=float(
                raw.get("unknown_family_quality", cls.unknown_family_quality)
            ),
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "family_weight": self.family_weight,
            "ema_weight": self.ema_weight,
            "curation_weight": self.curation_weight,
            "unknown_family_quality": self.unknown_family_quality,
        }


class ModelScorer:
    """Computes a 0..1 quality score for a candidate model.

    Constructed once per dispatcher. ``reload()`` re-reads the family-tier
    override file and the scoring config so edits take effect without a restart.
    """

    def __init__(
        self,
        config: ScoringConfig | None = None,
        tiers_path: Path | None = None,
    ) -> None:
        self._config = config or ScoringConfig()
        self._tiers_path = tiers_path or (Path.home() / ".north" / "model_tiers.json")
        self._tiers: dict[str, float] = dict(_DEFAULT_TIERS)
        self.reload()

    # ---- live reloading ----

    def reload(self) -> None:
        """Re-read the family-tier override file. Keeps built-in defaults as base."""
        self._tiers = dict(_DEFAULT_TIERS)
        if self._tiers_path and self._tiers_path.exists():
            try:
                overrides = json.loads(self._tiers_path.read_text(encoding="utf-8"))
                if isinstance(overrides, dict):
                    for k, v in overrides.items():
                        try:
                            self._tiers[str(k).lower()] = float(v)
                        except (TypeError, ValueError):
                            logger.warning("Bad family-tier entry %r=%r; skipped", k, v)
            except Exception as exc:  # corrupt override file -> keep defaults
                logger.warning("Could not read %s: %s", self._tiers_path, exc)

    def set_config(self, config: ScoringConfig) -> None:
        self._config = config

    # ---- scoring ----

    def family_tier(self, model_id: str) -> float:
        """Longest matching substring wins; unknown falls back to config floor."""
        lower = model_id.lower()
        best_len, best_score = 0, self._config.unknown_family_quality
        for key, score in self._tiers.items():
            if key in lower and len(key) > best_len:
                best_len, best_score = len(key), score
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
